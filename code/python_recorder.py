import pyautogui
import cv2
import numpy as np
import atexit

import getpass
from datetime import datetime
from screeninfo import get_monitors
import os
import ctypes
import ping3
import psutil
def get_ethernet_ip_address():
    interfaces = psutil.net_if_addrs()
    return(interfaces['乙太網路'][1][1])
def check_ip_status(ip):
    result = ping3.ping(ip)
    return result is not None
def show_warning(message):
    ctypes.windll.user32.MessageBoxW(0, message, "警告", 0x30)
ip_to_check = "192.168.7.90"
if not check_ip_status(ip_to_check):
    show_warning("NAS無法連線")
else:
    ip_address = get_ethernet_ip_address()
    username = getpass.getuser()
    current_datetime = datetime.now()
    formatted_datetime = current_datetime.strftime("%Y-%m-%d_%H-%M-%S")

    monitors = get_monitors()
    if monitors:
        screen = monitors[0] 
        screen_width, screen_height = screen.width, screen.height
    else:
        screen_width, screen_height = 1920, 1080  # 

    # Specify resolution
    resolution = (1920, 1080)
    codec = cv2.VideoWriter_fourcc(*"XVID")

    # Specify name of Output file

    directory_test = r'\\192.168.7.90\videos\{}'.format(ip_address.replace('.', '_'))
    if not os.path.exists(directory_test):
        try:
            os.makedirs(directory_test)
            print(f"目錄 '{directory_test}' 建立成功")
        except OSError as e:
            print(f"建立目錄 '{directory_test}' 失敗： {e}")
            show_warning(f"建立目錄 '{directory_test}' 失敗： {e}")
    else:
        print(f"目錄 '{directory_test}' 已經存在")
    filename_stamp = username + '_' + ip_address.replace('.', '_') + '_' + formatted_datetime
    filename = f"\\\\192.168.7.90\\videos\\{ip_address.replace('.', '_')}\\{filename_stamp}.avi"
    fps = 20.0
    out = cv2.VideoWriter(filename, codec, fps, resolution)
    cv2.namedWindow("Live", cv2.WINDOW_GUI_NORMAL)
    cv2.setWindowProperty("Live", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    cv2.resizeWindow("Live", 320, 180)
    new_x, new_y = screen_width - 320, 0  
    cv2.moveWindow("Live", new_x, new_y)

    def save_video():
        out.release()

    # Register the save_video function to run on exit
    atexit.register(save_video)

    while True:
        img = pyautogui.screenshot()
        frame = np.array(img)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        out.write(frame)
        cv2.imshow('Live', frame)

        # Stop recording when we press 'q'
        if cv2.waitKey(1) == ord('*'):
            break
    out.release()
    cv2.destroyAllWindows()
