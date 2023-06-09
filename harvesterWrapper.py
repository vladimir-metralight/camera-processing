from harvesters.core import Harvester
import numpy as np
import cv2
import logging
import time
import hjson
import threading
import queue
from pyee.asyncio import AsyncIOEventEmitter
from harvesters.util.pfnc import mono_location_formats, \
    rgb_formats, bgr_formats, \
    rgba_formats, bgra_formats, bayer_location_formats

ACCESS_STATUSES = {
    0 : "UNKN",
    1 : "READY",
    2 : "READ_ONLY",
    3 : "UNREACHABLE",
    4 : "BUSY",
}
class HarvesterWrapper(AsyncIOEventEmitter):
    config = None

    harvester = None
    ia = None

    grabThread = None
    grabStoppedEvent = threading.Event()

    def __init__(self, config):
        super().__init__()

        self.config = config
        self.imageLock = threading.Lock()
        self.image = None

        self.harvester = Harvester()
        for cti in self.config["CTI"]:
            self.harvester.add_file(cti)
        self.harvester.update()
        
        if len(self.harvester.device_info_list) == 0:
            raise Exception("No devices")

        logging.info("Imaging devices:")
        for dev in self.harvester.device_info_list:
            logging.info(dev)

    
        # find required model or serial_number
        # model STC_CMC4MPOE
        # serial 15D2184

    def isCapturing(self):
        return self.ia is not None and self.ia.is_acquiring()

    def getDevices(self):
        self.harvester.update()
        res = []
        for dev in self.harvester.device_info_list:
            res.append({
                "model" : dev.model,
                "serial_number" : dev.serial_number,
                "display_name" : dev.display_name,
                "access_status" : ACCESS_STATUSES[dev.access_status],
                "vendor" : dev.vendor,
            })
        return res
    
    def getImage(self):
        with self.imageLock:
            if self.image is None:
                return None
            else:
                return np.copy(self.image)

    def updateNode(self, nodeName, value):
        self._setNode(nodeName, value, throw=True)

        # vratit vsechny nodes updatovane
        return self.getUserConfigNodes()
    
    def getUserConfigNodes(self):
        # ulozit konfigurovatelne nodes
        userNodes = []
        for prop in self.config["USER_NODES"]:
            node = None
            try:
                node = self.ia.remote_device.node_map.get_node(prop)
            except Exception as err:
                logging.warning(f"Can not get node: {prop}", exc_info=True)
            if node is not None:
                userNodes.append(node)
        
        # puvodne reformat nodes az v app, ale nefungovalo
        # nejspis zamrlo kvuli asynchronne spustenemu videu
        userNodes = self._harvestNodesToPython(userNodes)

        return userNodes
    
    def startGrab(self, deviceInfo, userConfig=None):
        """
            V threadu nastartuje vycitani obrazu z kamery
            Vrati seznam konfigurovatelnych nodes
        """
        logging.info(f"StartGrab deviceInfo={deviceInfo} userConfig={userConfig}")
        
        self.grabStoppedEvent.clear()
        try:
            self.ia = self.harvester.create({"model" : deviceInfo["model"], "serial_number" :  deviceInfo["serial_number"]})
        except Exception as e:
            raise Exception("Can not access camera defined by identifier")

        # set default config
        for prop in self.config["DEFAULT_CONFIG"]:
            self._setNode(prop, self.config["DEFAULT_CONFIG"][prop], throw=False)
        
        # pokud je userConfig tak také nastavit
        if userConfig is not None:
            for prop in userConfig:
                self._setNode(prop, userConfig[prop], throw=False)

        # ulozit konfigurovatelne nodes
        userNodes = self.getUserConfigNodes()

        self.grabThread = threading.Thread(target=self._grabbingWork)
        self.grabThread.setDaemon(True)
        self.grabThread.start()

        return userNodes


    def stopGrab(self):
        self.grabStoppedEvent.set()
        self.grabThread.join() #zajisti ze se fce vrati az po skonceni read threadu

        if self.ia:
            self.ia.destroy()

        self.image = None

    def _grabbingWork(self):
        self.ia.start()
        while not self.grabStoppedEvent.isSet():
            try:
                with self.ia.fetch(timeout=self.config["FRAME_READ_TIMEOUT"]) as buffer:
                    # Work with the Buffer object. It consists of everything you need.
                    payload = buffer.payload
                    component = payload.components[0]
                    width = component.width
                    height = component.height
                    data_format = component.data_format

                    # Reshape the image to rgb always
                    if data_format in mono_location_formats:
                        content = component.data.reshape(height, width)
                        tmp = np.zeros((content.shape[0], content.shape[1], 3), dtype=content.dtype)
                        tmp[:,:,0] = content
                        tmp[:,:,1] = content
                        tmp[:,:,2] = content
                        content = tmp
                    else:
                        if data_format in rgb_formats or \
                                data_format in rgba_formats or \
                                data_format in bgr_formats or \
                                data_format in bgra_formats or \
                                data_format in bayer_location_formats:
                            content = component.data.reshape(
                                height, width,
                                int(component.num_components_per_pixel)  # Set of R, G, B, and Alpha
                            )
                            if data_format in bgr_formats:
                                # Swap every R and B:
                                content = content[:, :, ::-1]

                            elif data_format=="BayerBG8":
                                content = cv2.cvtColor(content, cv2.COLOR_BAYER_BG2RGB)
                            elif data_format=="BayerRG8":
                                content = cv2.cvtColor(content, cv2.COLOR_BayerRG2RGB)
                            elif data_format=="BayerGB8":
                                content = cv2.cvtColor(content, cv2.COLOR_BayerGB2RGB)
                            elif data_format=="BayerGR8": 
                                content = cv2.cvtColor(content, cv2.COLOR_BayerGR2RGB)
                            else:
                                raise Exception("Bayer pixel format not implemented")                         
                        else:
                            raise Exception("Pixel format not implemented")
                        
                    logging.info(f"Grabbed dimensions: {content.shape}")

                    self.emit("image", content)
                    # self.queue.put(content)
                    with self.imageLock:
                        self.image = np.copy(content)

            except Exception as e:
                logging.exception("Exception during acquiring image")

        self.ia.stop()

    def _harvestNodesToPython(self, nodes):
        resultArr = []
        for node in nodes:
            obj = {}
            obj["name"] = node.node.name
            obj["display_name"] = node.node.display_name
            obj["tooltip"] = node.node.tooltip
            obj["value"] = node.value

            # select pokud ma entries
            if hasattr(node, "entries"):
                obj["type"] = "select"
                obj["options"] = []
                for entry in node.entries:
                    obj["options"].append({
                        "display_name" : entry.symbolic,
                        "value" : entry.value
                    })
            # jinak cislo
            else:
                obj["type"] = "number"
                obj["min"] = node.min
                obj["max"] = node.max
                obj["unit"] = node.unit

            resultArr.append(obj)

        return resultArr
    
    def _setNode(self, propName, value, throw=False):
        node = None
        try:
            node = self.ia.remote_device.node_map.get_node(propName)
        except Exception as err:
            msg = f"Can not get node: {propName}"
            logging.warning(msg, exc_info=True)
            if throw:
                raise Exception(msg)
            
        if node is not None:
            try:
                node.value = value
            except:
                msg = f"Can not set value: {value} to node: {propName}"
                logging.warning(msg, exc_info=True)
                if throw:
                    raise Exception(msg)

if __name__ == '__main__':
    rootLogger = logging.getLogger()
    rootLogger.setLevel(10)

    config = hjson.load(open("cameraConfig.hjson", "r"))
    cam = HarvesterWrapper(config)

    def show(image):
        cv2.imshow("test", image)
        cv2.waitKey(1)

    cam.on("image", show)
    cam.startGrab({"model" : "STC_CMC4MPOE"})
    time.sleep(5)
    cam.stopGrab()
    