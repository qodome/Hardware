# -*- coding: utf-8 -*-
'''
This is the main program for iDo production flash/verification
Before testing, make sure
1. TI flash programmer has connected
2. BLE USB dongle has connected

For each unit:
1. Erase flash and burn hex.
2. Verify BLE connection with USB dongle, read back one temperature.
'''

import wx
import wx.lib.platebtn as platebtn
import itertools
import _winreg
import _winreg as reg
from itertools import count
import wmi
import re
import sys
import subprocess
from subprocess import check_output
import os
import serial
from threading import Thread
import time
from pyblehci import BLEBuilder
from pyblehci import BLEParser
import datetime

logFd = None
bleDongle = None
toolPath = "\"C:\\Program Files\\Texas Instruments\\SmartRF Tools\\Flash Programmer\\bin\\SmartRFProgConsole.exe\""
logPath = "logs"
taskStopSignal = 0
taskTriggerFlash = 0
taskTriggerTest = 0
frame = None
ble_builder = 0
ble_parser = 0
hci_cmd = ""
hci_cmd_status = 0
hci_cmd_rsp = ""
hci_cmd_rsp_cmd = ""
sample_data = 0.0

def analyse_packet((packet, dictionary)):
    global ble_builder
    global hci_cmd
    global hci_cmd_status
    global hci_cmd_rsp
    global hci_cmd_rsp_cmd
    global sample_data

    log("analyse_packet got event: " + dictionary['event'][1])
    if dictionary['event'][1] == "GAP_HCI_ExtensionCommandStatus":
        #print dictionary['op_code'][1] + " " + BLESerialGetCodeDesc(hci_cmd)
        if dictionary['op_code'][1] == BLESerialGetCodeDesc(hci_cmd):
            if dictionary['status'][1] == "00":
                hci_cmd_status = 0
            else:
                hci_cmd_status = 1
                log("HCI cmd " + hci_cmd + " extension cmd status: " + dictionary['status'][1])

    if dictionary['event'][1] == "GAP_DeviceInitDone":
        hci_cmd_rsp_cmd = "fe00"
        if dictionary['status'][1] == "00":
            hci_cmd_rsp = "done"
        else:
            hci_cmd_rsp = "fail"

    if dictionary['event'][1] == "GAP_EstablishLink":
        hci_cmd_rsp_cmd = "fe09"
        if dictionary['status'][1] == "00":
            hci_cmd_rsp = "done"
        else:
            hci_cmd_rsp = "fail"

    if dictionary['event'][1] == "ATT_WriteRsp":
        hci_cmd_rsp_cmd = "fd92"
        if dictionary['status'][1] == "00":
            hci_cmd_rsp = "done"
        else:
            hci_cmd_rsp = "fail"

    if dictionary['event'][1] == "GAP_LinkTerminated":
        hci_cmd_rsp_cmd = "fe0a"
        if dictionary['status'][1] == "00":
            hci_cmd_rsp = "done"
        else:
            hci_cmd_rsp = "fail"

    if dictionary['event'][1] == "ATT_HandleValueNotification":
        values_list = list(dictionary['values'][0])
        temp = (ord(values_list[3]) << 16) | (ord(values_list[2]) << 8) | ord(values_list[1])
        sample_data = temp/10000.0

def BLESerialGetCodeDesc(cmd):
    if cmd == "fe00":
        return "GAP_DeviceInit"
    elif cmd == "fe09":
        return "GATT_EstablishLinkRequest"
    elif cmd == "fd92":
        return "GATT_WriteCharValue"
    elif cmd == "fe0a":
        return "GATT_TerminateLinkRequest"

def BLESerialCmdWaitTime(cmd):
    if cmd == "fe00":
        return 1
    elif cmd == "fe09":
        return 15
    elif cmd == "fd92":
        return 15
    elif cmd == "fe0a":
        return 15

def BLESerialCmd(cmd, skip_cmd):
    global ble_builder
    global hci_cmd
    global hci_cmd_status
    global hci_cmd_rsp
    global hci_cmd_rsp_cmd

    hci_cmd = cmd
    hci_cmd_status = -1
    hci_cmd_rsp = ""
    hci_cmd_rsp_cmd = ""

    if skip_cmd == 0:
        ble_builder.send(cmd)

    # Check the cmd status after 0.5 second
    time.sleep(0.5)
    if hci_cmd_status == -1:
        # CMD timeout
        log("HCI cmd " + cmd + " timeout!")
        return 1
    elif hci_cmd_status != 0:
        # CMD returned error
        log("HCI cmd " + cmd + " timeout!")
        return 1
    
    count = 0
    while count < BLESerialCmdWaitTime(cmd):
        if hci_cmd_rsp != "":
            break
        time.sleep(1)
        count = count + 1

    if hci_cmd_rsp == "done" and hci_cmd_rsp_cmd == cmd:
        return 0
    else:
        log("HCI cmd " + cmd + " failed")
        return 1

def log(msg):
    global logFd
    logFd.write(str(datetime.datetime.now()) + " " + msg + "\n")
    logFd.flush()
    #print msg

def TriggerFlash():
    global taskTriggerFlash
    taskTriggerFlash = 1

def TriggerTest():
    global taskTriggerTest
    taskTriggerTest = 1

def CheckFlashProgram():
    errorMsg = "烧写失败！"

    programmerStatus = check_output(toolPath + " X", shell = True)
    if "Device:CC Debugger" not in programmerStatus:
        errorMsg = "烧写失败，请检查烧写设备和\n电脑的连接！"
    elif "Chip:CC2541" not in programmerStatus:
        errorMsg = "烧写失败，请确认板子和治具\n的接触良好！"

    return errorMsg

def Task(i):
    global taskStopSignal
    global taskTriggerFlash
    global taskTriggerTest
    global frame
    global ble_builder
    global ble_parser
    global sample_data

    while True:
        # Wait 
        while taskTriggerFlash == 0:
            time.sleep(0.1)
            if taskStopSignal == 1:
                break

        taskTriggerFlash = 0

        if taskStopSignal == 1:
            log("Task exiting...")
            ble_parser.stop()
            break
        
        log("\n\nTask started")

        # Chip full erase
        try:
            programmerStatus = check_output(toolPath + " S CE", shell = True)
        except subprocess.CalledProcessError as exc:                                                                                                   
            if exc.returncode == 5:
                log("communication error")
                msg = "请按烧录器按钮后重试！"
            else:
                msg = "烧写失败！"
            log("Status : FAIL " + str(exc.returncode) + " " + exc.output)
            log("Failed to do chip erase")
            frame.TaskUpdateFlashStatus(1, msg)
            continue
        else:
            if "Chip erased OK" in programmerStatus:
                pass
            else:
                log("Chip Erase failed")
                errMsg = CheckFlashProgram()
                frame.TaskUpdateFlashStatus(1, errMsg)
                continue

        # Query chip MAC address
        addr = 0
        try:
            macQueryRsp = check_output(toolPath + " S RI(F=256)", shell = True)
        except subprocess.CalledProcessError as exc:                                                                                                   
            log("Status : FAIL " + str(exc.returncode) + " " + exc.output)
            log("Query MAC address failed")
            frame.TaskUpdateFlashStatus(1, "烧写失败！")
            continue
        else:
            if "IEEE MAC address read successfully" not in macQueryRsp:
                log("Query MAC address failed")
                frame.TaskUpdateFlashStatus(1, "烧写失败！")
                continue
            elif "00.00.00.00.00.00" in macQueryRsp:
                log("Query MAC address zero")
                frame.TaskUpdateFlashStatus(1, "烧写失败！")
                continue
            else:
                lines = macQueryRsp.splitlines()
                fields = lines[len(lines) - 1].split(" ")
                elements = fields[len(fields) - 1].split(".")
                addr = chr(int(elements[5], 16)) +  \
                        chr(int(elements[4], 16)) + \
                        chr(int(elements[3], 16)) + \
                        chr(int(elements[2], 16)) + \
                        chr(int(elements[1], 16)) + \
                        chr(int(elements[0], 16))

        # Do the flash task
        try:
            programmerStatus = check_output(toolPath + " S EP F=\"C:\\_Software_\\iDo_production_test\\res\\combined.hex\" LD", shell = True)
        except subprocess.CalledProcessError as exc:                                                                                                   
            if exc.returncode == 5:
                log("communication error")
                msg = "请按烧录器按钮后重试！"
            else:
                msg = "烧写失败！"
            log("Status : FAIL " + str(exc.returncode) + " " + exc.output)
            log("Failed to flash CC2541")
            frame.TaskUpdateFlashStatus(1, msg)
            continue
        else:
            if "Erase and program OK" in programmerStatus:
                pass
            else:
                log("Flash EP failed")
                errMsg = CheckFlashProgram()
                frame.TaskUpdateFlashStatus(1, errMsg)
                continue

        frame.TaskUpdateFlashStatus(0, "烧写通过！")
        log("Flash good")

        # Notify flash result and wait on Test action
        while taskTriggerTest == 0:
            time.sleep(0.1)
            if taskStopSignal == 1:
                break

        taskTriggerTest = 0

        if taskStopSignal == 1:
            log("Task exiting...")
            ble_parser.stop()
            break

        # BLE dongle test if flash ok
        log("Connecting to " + fields[len(fields) - 1].replace(".", ":"))
        ble_builder.send("fe09", peer_addr = addr)
        if BLESerialCmd("fe09", 1) != 0:
            log("Failed to connect to device")
            frame.TaskUpdateTestStatus(1, "测试失败！")
            continue
        else:
            log("BLE device connected.")
            time.sleep(1)
            # Enable notification
            sample_data = 0.0
            ble_builder.send("fd92", handle = "\x16\x00", value = "\x01\x00")
            if BLESerialCmd("fd92", 1) != 0:
                log("Failed to enable notification")
                frame.TaskUpdateTestStatus(1, "测试失败！")
                continue
            else:
                log("BLE device notification ON")
                time.sleep(3)

                if BLESerialCmd("fe0a", 0) != 0:
                    log("Failed to terminate BLE link")

                if sample_data < 40.0 and sample_data > 10.0:
                    log("Test good!")
                    frame.TaskUpdateTestStatus(0, "测试通过！")
                else:
                    log("Read temperature: " + str(sample_data) + " not in valid range, fail!")
                    frame.TaskUpdateTestStatus(1, "测试失败！")
                    continue

class MainFrame(wx.Frame):
    serialPort = None
    task = None
    
    def __init__(self, title="温度贴烧录测试工具v0.1", size=[400, 400]):
        global bleDongle
        global toolPath
        global ble_builder
        global ble_parser

        wx.Frame.__init__(self,parent=None, style=wx.CAPTION | wx.SYSTEM_MENU | wx.CLOSE_BOX, title=title, size=size) 
        
        self.Bind(wx.EVT_CLOSE, self.OnClose)
        self.SetBackgroundColour((110, 110, 110))

        panel = wx.Panel(self, -1)  
        self.buttonFlash = wx.Button(panel, -1, "开始烧写", size=(150, 60), pos=(40, 250))  
        font = wx.Font(18, wx.DEFAULT, wx.NORMAL, wx.FONTWEIGHT_BOLD)
        self.buttonFlash.SetFont(font)
        self.txtMsg = wx.StaticText(panel, -1, "", pos=(30, 50))
        self.txtMsg.SetFont(font)
        self.Bind(wx.EVT_BUTTON, self.OnFlashAction, self.buttonFlash)  
        self.buttonFlash.SetDefault() 
    
        self.buttonTest = wx.Button(panel, -1, "开始测试", size=(150, 60), pos=(200, 250))  
        self.buttonTest.SetFont(font)
        self.Bind(wx.EVT_BUTTON, self.OnTestAction, self.buttonTest)  
        self.buttonTest.SetDefault() 
        self.buttonTest.Disable()

        # Check to make sure dongle and programmer is available
        # Check available CC2540 USB dongle
        dcom = ""
        pattern = re.compile('COM[0-9]+')
        ti_ble = re.compile('CC2540')
        c = wmi.WMI()
        wql = "Select * From Win32_USBControllerDevice"
        for item in c.query(wql):
            if ti_ble.search(item.Dependent.Caption) != None and pattern.search(item.Dependent.Caption) != None:
                dcom = pattern.search(item.Dependent.Caption).group()
                log("Got Dongle port: " + dcom)
        if dcom == "":
            log("TI BLE USB Dongle not found, exiting...")
            dlg = wx.MessageDialog(None, "没有找到蓝牙Dongle，请插入电脑后重试！", "警告", wx.OK | wx.ICON_WARNING)
            dlg.ShowModal()
            dlg.Destroy()
            sys.exit(1)

        try:
            bleDongle = serial.Serial(port=dcom, baudrate=115200, writeTimeout=1.0)
            if bleDongle == None:
                dlg = wx.MessageDialog(None, "蓝牙Dongle正在使用，请关闭其他应用程序再试！", "警告", wx.OK | wx.ICON_WARNING)
                dlg.ShowModal()
                dlg.Destroy()
                sys.exit(1)

        except serial.SerialException:
            log("Failed to open dongle, maybe in use?")
            dlg = wx.MessageDialog(None, "蓝牙Dongle正在使用，请关闭其他应用程序再试！", "警告", wx.OK | wx.ICON_WARNING)
            dlg.ShowModal()
            dlg.Destroy()
            sys.exit(1)

        except UnicodeDecodeError:
            log("Failed to open dongle, maybe in use?")
            dlg = wx.MessageDialog(None, "蓝牙Dongle正在使用，请关闭其他应用程序再试！", "警告", wx.OK | wx.ICON_WARNING)
            dlg.ShowModal()
            dlg.Destroy()
            sys.exit(1)

        programmerStatus = check_output(toolPath + " X", shell = True)
        if "Device:CC Debugger" not in programmerStatus:
            log("SmartRF programmer not found")
            dlg = wx.MessageDialog(None, "没有找到TI烧录器，请插上后重试！", "警告", wx.OK | wx.ICON_WARNING)
            dlg.ShowModal()
            dlg.Destroy()
            sys.exit(1)


        # Initialize BLE Dongle
        ble_builder = BLEBuilder(bleDongle)
        ble_parser = BLEParser(bleDongle, callback=analyse_packet)

        #initialise the device
        if BLESerialCmd("fe00", 0) != 0:
            log("Failed to initialize BLE dongle!")
            ble_parser.stop()
            dlg = wx.MessageDialog(None, "蓝牙Dongle无法初始化，\n请重插后再试！", "警告", wx.OK | wx.ICON_WARNING)
            dlg.ShowModal()
            dlg.Destroy()
            sys.exit(1)
        else:
            log("BLE dongle initialize done!")

        self.task = Thread(target = Task, args=(0,))
        self.task.start()

    def OnClose(self, event):
        global taskStopSignal
        taskStopSignal = 1
        log("Waiting on thread exit...")
        self.task.join()
        self.Destroy()
    
    def OnFlashAction(self, event):
        self.txtMsg.SetLabel("烧录进行中...")
        self.txtMsg.SetForegroundColour((0, 0, 0))
        TriggerFlash()
        self.buttonFlash.Disable()
        self.buttonTest.Disable()

    def OnTestAction(self, event):
        self.txtMsg.SetLabel("测试进行中...")
        self.txtMsg.SetForegroundColour((0, 0, 0))
        TriggerTest()
        self.buttonFlash.Disable()
        self.buttonTest.Disable()

    def TaskUpdateFlashStatus(self, status, msg):
        self.txtMsg.SetLabel(msg)
        if status == 1:
            self.txtMsg.SetForegroundColour((255, 0, 0))
        else:
            self.txtMsg.SetForegroundColour((0, 255, 0))

        if status == 1:
            self.buttonFlash.Enable()
            self.buttonTest.Disable()
        else:
            self.buttonFlash.Disable()
            self.buttonTest.Enable()

    def TaskUpdateTestStatus(self, status, msg):
        self.txtMsg.SetLabel(msg)
        if status == 1:
            self.txtMsg.SetForegroundColour((255, 0, 0))
        else:
            self.txtMsg.SetForegroundColour((0, 255, 0))

        self.buttonFlash.Enable()
        self.buttonTest.Disable()

if __name__ == '__main__':
    if not os.path.exists(logPath):
        os.makedirs(logPath)

    fn = "flash_ut-" + time.strftime('%Y-%m-%d_%H_%M_%S',time.localtime(time.time())) + ".log"
    logFd = open(logPath + "/" + fn, "w")
    if logFd == None:
        print "Failed to open " + fn + " for write log"
        sys.exit()

    app = wx.PySimpleApp()
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
