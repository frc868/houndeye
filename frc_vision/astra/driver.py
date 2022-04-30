import enum
import logging
import pickle
import socket
import struct
import subprocess
import threading
import time
import typing

import cscore
import cv2
import networktables
import numpy as np
from cscore import CameraServer
from networktables import NetworkTables
from openni import _openni2 as c_api
from openni import openni2

import frc_vision.astra.utils
import frc_vision.calibration
import frc_vision.constants
import frc_vision.utils
import frc_vision.viewer
from frc_vision.utils import circles, cv2Frame

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Helper classes


class TableGroup:
    FRCVision: networktables.NetworkTable
    SmartDashboard: networktables.NetworkTable
    FMSInfo: networktables.NetworkTable


class AstraException(Exception):
    pass


class Alliance(enum.IntEnum):
    BLUE = 0
    RED = 1


class Driver:
    """Main class that runs the Astra. Uses util functions from `frc_vision.astra.utils`."""

    color_stream: typing.Optional[openni2.VideoStream]
    depth_stream: typing.Optional[openni2.VideoStream]
    tables: TableGroup = TableGroup()
    client: socket.SocketIO
    enable_calibration: bool
    enable_networking: bool
    cs: CameraServer
    cs_output: None
    alliance: Alliance = Alliance.BLUE

    def __init__(
        self, enable_calibration: bool = False, enable_networking: bool = True
    ):
        self.create_streams()

        self.client = None

        self.enable_calibration = enable_calibration
        self.enable_networking = enable_networking

        if self.enable_networking:
            self.initialize_networktables()
            self.initialize_cameraserver()

    def create_streams(self) -> None:
        """
        Initializes and synchronized the color and depth streams from an
        Orbbec Astra camera through OpenNI.
        """
        logger.info("Initializing OpenNI")
        openni2.initialize(dll_directories=["./openni-redist"])

        logger.info("Opening device")
        device = openni2.Device.open_any()

        logger.info("Creating color stream")
        self.color_stream = device.create_color_stream()
        self.camera_settings = openni2.CameraSettings(self.color_stream)
        self.camera_settings.set_auto_exposure(False)
        self.camera_settings.set_auto_white_balance(False)
        self.color_stream.set_video_mode(
            c_api.OniVideoMode(
                pixelFormat=c_api.OniPixelFormat.ONI_PIXEL_FORMAT_RGB888,
                resolutionX=frc_vision.constants.ASTRA.RESOLUTION_W,
                resolutionY=frc_vision.constants.ASTRA.RESOLUTION_H,
                fps=frc_vision.constants.ASTRA.FPS,
            )
        )
        self.color_stream.start()

        # pixelFormat can also be "ONI_PIXEL_FORMAT_DEPTH_1_MM"
        logger.info("Creating depth stream")
        self.depth_stream = device.create_depth_stream()
        self.depth_stream.set_video_mode(
            c_api.OniVideoMode(
                pixelFormat=c_api.OniPixelFormat.ONI_PIXEL_FORMAT_DEPTH_100_UM,
                resolutionX=frc_vision.constants.ASTRA.RESOLUTION_W,
                resolutionY=frc_vision.constants.ASTRA.RESOLUTION_H,
                fps=frc_vision.constants.ASTRA.FPS,
            )
        )
        self.depth_stream.start()

        logger.info("Synchronizing color and depth sensors")
        device.set_image_registration_mode(openni2.IMAGE_REGISTRATION_DEPTH_TO_COLOR)
        device.set_depth_color_sync_enabled(True)

    def initialize_networktables(self):
        """Connects to NetworkTables on the roboRIO."""
        NetworkTables.initialize(server=frc_vision.constants.SERVERS.ROBORIO_SERVER_IP)
        self.tables.FRCVision = NetworkTables.getTable("FRCVision")
        self.tables.SmartDashboard = NetworkTables.getTable("SmartDashboard")
        self.tables.FMSInfo = NetworkTables.getTable("FMSInfo")
        self.set_alliance()

    def initialize_cameraserver(self):
        CameraServer.enableLogging()
        self.cs = CameraServer()
        self.cs_output = self.cs.putVideo("Astra", 320, 240)

    def get_frames(self) -> tuple[cv2Frame, cv2Frame]:
        """
        Reads the color and depth frames from an Orbbec Astra camera
        through OpenNI and converts it to an openCV-usable format.

        Args:
            None

        Returns:
            openCV color frame
            openCV depth frame
        """

        raw_color_frame = self.color_stream.read_frame()
        color_frame = np.frombuffer(
            raw_color_frame.get_buffer_as_uint8(), dtype=np.uint8
        )
        color_frame.shape = (
            frc_vision.constants.ASTRA.RESOLUTION_H,
            frc_vision.constants.ASTRA.RESOLUTION_W,
            3,
        )
        color_frame = cv2.cvtColor(color_frame, cv2.COLOR_BGR2RGB)
        color_frame = cv2.flip(color_frame, 1)

        raw_depth_frame = self.depth_stream.read_frame()
        depth_frame = np.frombuffer(
            raw_depth_frame.get_buffer_as_uint16(), dtype=np.uint16
        )
        depth_frame.shape = (
            frc_vision.constants.ASTRA.RESOLUTION_H,
            frc_vision.constants.ASTRA.RESOLUTION_W,
        )
        depth_frame = cv2.medianBlur(depth_frame, 3)
        depth_frame = cv2.flip(depth_frame, 1)

        return color_frame, depth_frame

    def destroy(self) -> None:
        """Cleans up streams and unloads camera."""
        self.depth_stream.stop()
        self.color_stream.stop()

        openni2.unload()
        cv2.destroyAllWindows()

    def write_to_networktables(self, data) -> None:
        """
        Writes circle location data to NetworkTables.

        CURRENT DATA STRUCTURE:
        Four arrays are output to NetworkTables, with
        indices being consistent across all arrays
        (that is, ball 0 will be ball 0 in color, tx, ty, and td)
        color: either "B" or "R", denotes ball color
        tx: x degree offset from center (from -30 to 30)
        ty: y degree offset from center (from -24.75 to 24.75)
        td: distance from camera to ball
        """
        tx, ty, td = data
        self.tables.FRCVision.putString(
            "alliance", "B" if self.alliance == Alliance.BLUE else "R"
        )
        self.tables.FRCVision.putNumberArray("tx", tx)
        self.tables.FRCVision.putNumberArray("ty", ty)
        self.tables.FRCVision.putNumberArray("td", td)

    def process_frame(
        self, color_frame: cv2Frame, depth_frame: cv2Frame
    ) -> tuple[circles, circles, tuple[float]]:
        """
        Run all processing on the frames and return
        the end result. (Not decided yet)
        """
        blue_mask, red_mask = frc_vision.astra.utils.generate_masks(color_frame)
        blue_circles = frc_vision.astra.utils.find_circles(blue_mask, depth_frame)
        red_circles = frc_vision.astra.utils.find_circles(red_mask, depth_frame)

        txb, tyb = frc_vision.astra.utils.calculate_angles(blue_circles)
        txr, tyr = frc_vision.astra.utils.calculate_angles(red_circles)

        tdb = [d for x, y, r, d in blue_circles]
        tdr = [d for x, y, r, d in red_circles]

        if self.alliance == Alliance.BLUE:
            data = frc_vision.astra.utils.zip_networktables_data(txb, tyb, tdb)
        else:
            data = frc_vision.astra.utils.zip_networktables_data(txr, tyr, tdr)

        if self.enable_networking:
            self.write_to_networktables(data)
        return blue_circles, red_circles, data

    def send_data(self, frame, blue_circles, red_circles, start_time):
        """Sends frame data with annotations to the driver's station."""
        frame = frc_vision.viewer.draw_circles(frame, blue_circles, red_circles)
        frame = frc_vision.viewer.draw_metrics(frame, start_time)
        frame = cv2.resize(frame, (160, 120))
        self.cs_output.putFrame(frame)

    def write_rpi_temps(self):
        """Runs `vcgencmd measure_temp` to get the current temperature of the Pi and sends it to SmartDashboard."""
        raw_output = subprocess.run(
            ["vcgencmd", "measure_temp"], capture_output=True, text=True
        ).stdout
        trimmed_output = raw_output.lstrip("temp=").rstrip("'C\n")
        self.tables.SmartDashboard.putNumber("rpi_temp", float(trimmed_output))

    def set_alliance(self):
        """
        Checks FMSInfo to see what alliance is currently set.
        This can be changed in practice via the Driver Station.
        """
        self.alliance = (
            Alliance.RED
            if self.tables.FMSInfo.getBoolean("IsRedAlliance", True)
            else Alliance.BLUE
        )

    def run(self) -> None:
        """Main driver to run the detection program."""
        frc_vision.constants.load_constants()

        if self.enable_calibration:
            frc_vision.calibration.initalize_calibrators()

        # if self.enable_networking:
        #     conn_thread = threading.Thread(target=self.wait_for_connection)
        #     conn_thread.start()

        running = True
        while running:
            try:
                start_time = time.time()
                color_frame, depth_frame = self.get_frames()
                blue_circles, red_circles, data = self.process_frame(
                    color_frame, depth_frame
                )
                # print(time.time())

                tx, ty, ta = data

                self.camera_settings.set_exposure(frc_vision.constants.ASTRA.EXPOSURE)
                self.camera_settings.set_gain(frc_vision.constants.ASTRA.GAIN)

                if self.enable_networking:
                    self.send_data(color_frame, blue_circles, red_circles, start_time)
                    self.set_alliance()
                    self.write_rpi_temps()

                if self.enable_calibration:
                    blue_mask, red_mask = frc_vision.astra.utils.generate_masks(
                        color_frame
                    )

                    frc_vision.viewer.view(
                        frames=(
                            frc_vision.viewer.ViewerFrame(
                                color_frame, "color", show_data=True
                            ),
                            frc_vision.viewer.ViewerFrame(depth_frame, "depth"),
                            frc_vision.viewer.ViewerFrame(blue_mask, "blue"),
                            frc_vision.viewer.ViewerFrame(red_mask, "red"),
                        ),
                        circles=(blue_circles, red_circles),
                        data=(
                            frc_vision.viewer.ViewerData("tx", tx),
                            frc_vision.viewer.ViewerData("ty", ty),
                            frc_vision.viewer.ViewerData("ta", ta),
                        ),
                        start_time=start_time,
                    )

                    frc_vision.calibration.update_calibrators()

                if cv2.waitKey(15) == frc_vision.constants.KEYS.CV2_WAIT_KEY:
                    running = False
            except KeyboardInterrupt:
                running = False

        self.destroy()
