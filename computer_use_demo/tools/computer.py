import subprocess
import platform
import pyautogui
import asyncio
import base64
import os
if platform.system() == "Darwin":
    import Quartz  # uncomment this line if you are on macOS
from enum import StrEnum
from pathlib import Path
from typing import Literal, Tuple
from dataclasses import dataclass
from uuid import uuid4
from screeninfo import get_monitors
import adbutils

from PIL import ImageGrab, Image
from functools import partial

from anthropic.types.beta import BetaToolComputerUse20241022Param

from .base import BaseAnthropicTool, ToolError, ToolResult
from .run import run

OUTPUT_DIR = "./tmp/outputs"

TYPING_DELAY_MS = 12
TYPING_GROUP_SIZE = 50

Action = Literal[
    "key",
    "type",
    "mouse_move",
    "left_click",
    "left_click_drag",
    "right_click",
    "middle_click",
    "double_click",
    "screenshot",
    "cursor_position",
]


@dataclass
class Resolution():
    width: int
    height: int



class ScreenType(StrEnum):
    MONITOR = "monitor"
    ADB = "adb"

@dataclass
class Screen():
    index: int
    name: str
    type_: ScreenType
    size: Resolution
    layout: str
    position: str

    def __str__(self) -> str:
        match self.type_:
            case ScreenType.MONITOR:
                return f"Monitor {self.index}: {self.size.width}x{self.size.height}, {self.layout}, {self.position}"
            case ScreenType.ADB:
                return f"ADB Device {self.name}: {self.size.width}x{self.size.height}"
            case _:
                raise RuntimeError("unreachable")


MAX_SCALING_TARGETS: dict[str, Resolution] = {
    "XGA": Resolution(width=1024, height=768),  # 4:3
    "WXGA": Resolution(width=1280, height=800),  # 16:10
    "FWXGA": Resolution(width=1366, height=768),  # ~16:9
}


class ScalingSource(StrEnum):
    COMPUTER = "computer"
    API = "api"


@dataclass
class ComputerToolOptions():
    display_height_px: int
    display_width_px: int
    display_number: int | None


def chunks(s: str, chunk_size: int) -> list[str]:
    return [s[i : i + chunk_size] for i in range(0, len(s), chunk_size)]


def get_screen_details() -> Tuple[list[Screen], int]:
    screens = get_monitors()
    screen_details = []

    # Sort screens by x position to arrange from left to right
    sorted_screens = sorted(screens, key=lambda s: s.x)

    # Loop through sorted screens and assign positions
    primary_index = 0
    for i, screen in enumerate(sorted_screens):
        if i == 0:
            layout = "Left"
        elif i == len(sorted_screens) - 1:
            layout = "Right"
        else:
            layout = "Center"
        
        if screen.is_primary:
            position = "Primary" 
            primary_index = i
        else:
            position = "Secondary"
        screen_info = Screen(
            type_=ScreenType.MONITOR,
            index=i,
            name=None,
            size=Resolution(width=screen.width, height=screen.height),
            layout=layout,
            position=position
        )
        screen_details.append(screen_info)

    # Add Android Emulator screens via ADB if available
    adb = adbutils.AdbClient(host="127.0.0.1", port=5037)
    for device in adb.device_list():
        device_name = device.info['serialno']
        (width, height) = device.window_size()
        screen_info = Screen(
            type_=ScreenType.ADB,
            index=0,
            name=device_name,
            size=Resolution(width=width, height=height),
            layout=None,
            position=None,
        )
        screen_details.append(screen_info)

    return screen_details, primary_index


class ComputerTool(BaseAnthropicTool):
    """
    A tool that allows the agent to interact with the screen, keyboard, and mouse of the current computer.
    Adapted for Windows using 'pyautogui'.
    """

    name: Literal["computer"] = "computer"
    api_type: Literal["computer_20241022"] = "computer_20241022"
    width: int
    height: int
    display_num: int | None

    _screenshot_delay = 2.0
    _scaling_enabled = True

    _adb_device = None
    _adb_mouse = (0, 0)

    @property
    def options(self) -> ComputerToolOptions:
        width, height = self.scale_coordinates(
            ScalingSource.COMPUTER, self.width, self.height
        )
        return ComputerToolOptions(
            display_width_px=width,
            display_height_px=height,
            display_number=self.display_num,
        )

    def to_params(self) -> BetaToolComputerUse20241022Param:
        return {"name": self.name, "type": self.api_type, **self.options}

    def __init__(self, selected_screen: Screen = None):
        super().__init__()

        # Get screen width and height using Windows command
        self.display_num = None
        self.offset_x = 0
        self.offset_y = 0
        self.selected_screen = selected_screen
        resolution = self.selected_screen.size 
        self.width, self.height = resolution.width, resolution.height

        if selected_screen.type_ == ScreenType.ADB:
            adb = adbutils.AdbClient(host="127.0.0.1", port=5037)
            devices = adb.device_list()
            if not devices:
                raise RuntimeError("No Android devices found via ADB")
            
            device_name = selected_screen.name
            self._adb_device = next((d for d in devices if d.info['serialno'] == device_name), None)
            if self._adb_device is None:
                raise RuntimeError(f"No Android device found with serial number {device_name}")
            

        # Path to cliclick
        self.cliclick = "cliclick"
        self.key_conversion = {"Page_Down": "pagedown", "Page_Up": "pageup", "Super_L": "win"}

    async def __call__(
        self,
        *,
        action: Action,
        text: str | None = None,
        coordinate: tuple[int, int] | None = None,
        **kwargs,
    ):
        print(f"action: {action}, text: {text}, coordinate: {coordinate}")
        if action in ("mouse_move", "left_click_drag"):
            if coordinate is None:
                raise ToolError(f"coordinate is required for {action}")
            if text is not None:
                raise ToolError(f"text is not accepted for {action}")
            if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2:
                raise ToolError(f"{coordinate} must be a tuple of length 2")
            if not all(isinstance(i, int) and i >= 0 for i in coordinate):
                raise ToolError(f"{coordinate} must be a tuple of non-negative ints")

            x, y = self.scale_coordinates(
                ScalingSource.API, coordinate[0], coordinate[1]
            )
            x += self.offset_x
            y += self.offset_y

            if action == "mouse_move":
                if self._adb_device is not None:
                    self._adb_mouse = (x, y)
                else:
                    pyautogui.moveTo(x, y)
                return ToolResult(output=f"Moved mouse to ({x}, {y})")
            elif action == "left_click_drag":
                if self._adb_device is not None:
                    start_x, start_y = self._adb_mouse
                    self._adb_device.swipe(start_x, start_y, x, y, 0.5)
                    self._adb_mouse = (x, y)  # Update mouse position
                else:
                    current_x, current_y = pyautogui.position()
                    pyautogui.dragTo(x, y, duration=0.5)  # Adjust duration as needed
                return ToolResult(output=f"Dragged mouse from ({current_x}, {current_y}) to ({x}, {y})")

        if action in ("key", "type"):
            if text is None:
                raise ToolError(f"text is required for {action}")
            if coordinate is not None:
                raise ToolError(f"coordinate is not accepted for {action}")
            if not isinstance(text, str):
                raise ToolError(output=f"{text} must be a string")

            if action == "key":
                # Handle key combinations
                keys = text.split('+')
                if self._adb_device is not None:
                    # For ADB, send key events for each key
                    for key in keys:
                        key = key.lower()
                        self._adb_device.shell(f"input keyevent {' '.join(keys)}")
                else:
                    # For desktop, press and release keys in sequence
                    for key in keys:
                        key = self.key_conversion.get(key.strip(), key.strip())
                        key = key.lower()
                        pyautogui.keyDown(key)  # Press down each key
                    for key in reversed(keys):
                        key = self.key_conversion.get(key.strip(), key.strip())
                        key = key.lower()
                        pyautogui.keyUp(key)    # Release each key in reverse order
                return ToolResult(output=f"Pressed keys: {text}")
            
            elif action == "type":
                if self._adb_device is not None:
                    # For ADB, send text input directly
                    self._adb_device.send_keys(text)
                else:
                    # For desktop, use pyautogui typing
                    pyautogui.typewrite(text, interval=TYPING_DELAY_MS / 1000)  # Convert ms to seconds
                screenshot_base64 = (await self.screenshot()).base64_image
                return ToolResult(output=text, base64_image=screenshot_base64)

        if action in (
            "left_click",
            "right_click",
            "double_click",
            "middle_click",
            "screenshot",
            "cursor_position",
        ):
            if text is not None:
                raise ToolError(f"text is not accepted for {action}")
            if coordinate is not None:
                raise ToolError(f"coordinate is not accepted for {action}")

            if action == "screenshot":
                return await self.screenshot()
            elif action == "cursor_position":
                if self._adb_device is not None:
                    x, y = self._adb_mouse
                else:
                    x, y = pyautogui.position()
                x, y = self.scale_coordinates(ScalingSource.COMPUTER, x, y)
                return ToolResult(output=f"X={x},Y={y}")
            else:
                if action == "left_click":
                    if self._adb_device is not None:
                        x, y = self._adb_mouse
                        self._adb_device.shell(f"input tap {x} {y}")
                    else:
                        pyautogui.click()
                elif action == "right_click":
                    raise ToolError("right_click not supported for ADB devices")
                elif action == "middle_click":
                    raise ToolError("middle_click not supported for ADB devices") 
                elif action == "double_click":
                    raise ToolError("double_click not supported for ADB devices")
                return ToolResult(output=f"Performed {action}")
        raise ToolError(f"Invalid action: {action}")

    async def screenshot(self):
        """Take a screenshot of the current screen and return a ToolResult with the base64 encoded image."""
        output_dir = Path(OUTPUT_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"screenshot_{uuid4().hex}.png"

        if self.selected_screen.type_ == ScreenType.ADB:
            self.screenshot_from_adb(path, self.selected_screen.name)
        else:
            self.screenshot_from_monitor(path)


        if path.exists():
            # Return a ToolResult instance instead of a dictionary
            return ToolResult(base64_image=base64.b64encode(path.read_bytes()).decode())
        
        raise ToolError(f"Failed to take screenshot: {path} does not exist.")
    
    def screenshot_from_monitor(self, path):
        ImageGrab.grab = partial(ImageGrab.grab, all_screens=True)

        # Detect platform
        system = platform.system()

        if system == "Windows":
            # Windows: Use screeninfo to get monitor details
            screens = get_monitors()

            # Sort screens by x position to arrange from left to right
            sorted_screens = sorted(screens, key=lambda s: s.x)

            if self.selected_screen.index < 0 or self.selected_screen.index >= len(screens):
                raise IndexError("Invalid screen index.")

            screen = sorted_screens[self.selected_screen.index]
            bbox = (screen.x, screen.y, screen.x + screen.width, screen.y + screen.height)

        elif system == "Darwin":  # macOS
            # macOS: Use Quartz to get monitor details
            max_displays = 32  # Maximum number of displays to handle
            active_displays = Quartz.CGGetActiveDisplayList(max_displays, None, None)[1]

            # Get the display bounds (resolution) for each active display
            screens = []
            for display_id in active_displays:
                bounds = Quartz.CGDisplayBounds(display_id)
                screens.append({
                    'id': display_id,
                    'x': int(bounds.origin.x),
                    'y': int(bounds.origin.y),
                    'width': int(bounds.size.width),
                    'height': int(bounds.size.height),
                    'is_primary': Quartz.CGDisplayIsMain(display_id)  # Check if this is the primary display
                })

            # Sort screens by x position to arrange from left to right
            sorted_screens = sorted(screens, key=lambda s: s['x'])

            if self.selected_screen.index < 0 or self.selected_screen.index >= len(screens):
                raise IndexError("Invalid screen index.")

            screen = sorted_screens[self.selected_screen.index]
            bbox = (screen['x'], screen['y'], screen['x'] + screen['width'], screen['y'] + screen['height'])

        else:  # Linux or other OS
            cmd = "xrandr | grep ' primary' | awk '{print $4}'"
            try:
                output = subprocess.check_output(cmd, shell=True).decode()
                resolution = output.strip().split()[0]
                width, height = map(int, resolution.split('x'))
                bbox = (0, 0, width, height)  # Assuming single primary screen for simplicity
            except subprocess.CalledProcessError:
                raise RuntimeError("Failed to get screen resolution on Linux.")

        # Take screenshot using the bounding box
        screenshot = ImageGrab.grab(bbox=bbox)

        # Set offsets (for potential future use)
        self.offset_x = screen['x'] if system == "Darwin" else screen.x
        self.offset_y = screen['y'] if system == "Darwin" else screen.y

        if not hasattr(self, 'target_dimension'):
            screenshot = self.padding_image(screenshot)
            self.target_dimension = MAX_SCALING_TARGETS["WXGA"]

        # Resize if target_dimensions are specified
        print(f"offset is {self.offset_x}, {self.offset_y}")
        print(f"target_dimension is {self.target_dimension}")
        screenshot = screenshot.resize((self.target_dimension.width, self.target_dimension.height))

        # Save the screenshot
        screenshot.save(str(path))


    def screenshot_from_adb(self, path, device_name):
        self._adb_device.screenshot().save(str(path))


    def padding_image(self, screenshot):
        """Pad the screenshot to 16:10 aspect ratio, when the aspect ratio is not 16:10."""
        _, height = screenshot.size
        new_width = height * 16 // 10

        padding_image = Image.new("RGB", (new_width, height), (255, 255, 255))
        # padding to top left
        padding_image.paste(screenshot, (0, 0))
        return padding_image

    async def shell(self, command: str, take_screenshot=True) -> ToolResult:
        """Run a shell command and return the output, error, and optionally a screenshot."""
        _, stdout, stderr = await run(command)
        base64_image = None

        if take_screenshot:
            # delay to let things settle before taking a screenshot
            await asyncio.sleep(self._screenshot_delay)
            base64_image = (await self.screenshot()).base64_image

        return ToolResult(output=stdout, error=stderr, base64_image=base64_image)

    def scale_coordinates(self, source: ScalingSource, x: int, y: int):
        """Scale coordinates to a target maximum resolution."""
        if not self._scaling_enabled:
            return x, y
        ratio = self.width / self.height
        target_dimension = None

        for target_name, dimension in MAX_SCALING_TARGETS.items():
            # allow some error in the aspect ratio - not ratios are exactly 16:9
            if abs(dimension.width / dimension.height - ratio) < 0.02:
                if dimension.width < self.width:
                    target_dimension = dimension
                    self.target_dimension = target_dimension
                    # print(f"target_dimension: {target_dimension}")
                break

        if target_dimension is None:
            # TODO: currently we force the target to be WXGA (16:10), when it cannot find a match
            target_dimension = MAX_SCALING_TARGETS["WXGA"]
            self.target_dimension = MAX_SCALING_TARGETS["WXGA"]

        # should be less than 1
        x_scaling_factor = target_dimension.width / self.width
        y_scaling_factor = target_dimension.height / self.height
        if source == ScalingSource.API:
            if x > self.width or y > self.height:
                raise ToolError(f"Coordinates {x}, {y} are out of bounds")
            # scale up
            return round(x / x_scaling_factor), round(y / y_scaling_factor)
        # scale down
        return round(x * x_scaling_factor), round(y * y_scaling_factor)
    
    def get_mouse_position(self):
        # TODO: enhance this func
        from AppKit import NSEvent
        from Quartz import CGEventSourceCreate, kCGEventSourceStateCombinedSessionState

        loc = NSEvent.mouseLocation()
        # Adjust for different coordinate system
        return int(loc.x), int(self.height - loc.y)

    def map_keys(self, text: str):
        """Map text to cliclick key codes if necessary."""
        # For simplicity, return text as is
        # Implement mapping if special keys are needed
        return text