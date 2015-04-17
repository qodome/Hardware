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
logPath = "logs"
taskStopSignal = 0
taskAbortSignal = 0
taskTrigger = 0
frame = None
ble_builder = 0
ble_parser = 0
hci_cmd = ""
hci_cmd_status = 0
hci_cmd_rsp = ""
known_iDo_map = {}
new_iDo_count = 0
new_iDo_addr = ""
new_iDo_conn_handle = ""
discovery_done = 0
read_rsp_list = ()

def analyse_packet((packet, dictionary)):
    global ble_builder
    global hci_cmd
    global hci_cmd_status
    global hci_cmd_rsp
    global known_iDo_map
    global new_iDo_count
    global new_iDo_addr
    global new_iDo_conn_handle
    global discovery_done
    global hci_cmd_rsp_cmd
    global read_rsp_list

    log("analyse_packet got event: " + dictionary['event'][1])
    if dictionary['event'][1] == "GAP_HCI_ExtensionCommandStatus":
        #print dictionary['op_code'][1] + " " + BLESerialGetCodeDesc(hci_cmd)
        if dictionary['op_code'][1] == BLESerialGetCodeDesc(hci_cmd):
            if dictionary['status'][1] == "00":
                hci_cmd_status = 0
            else:
                hci_cmd_status = 1
                log("HCI cmd " + hci_cmd + " extension cmd status: " + dictionary['status'][1])

    if dictionary['event'][1] == "GAP_DeviceInformation":
        if dictionary['event_type'][1] == "04":
            data_field_list = list(dictionary['data_field'][0])
            if len(data_field_list) == 5 and data_field_list[2] == 'i' and data_field_list[3] == 'D' and data_field_list[4] == 'o':
                if not known_iDo_map.has_key(dictionary['addr'][1]):
                    log("Found new device " + dictionary['addr'][1])
                    known_iDo_map[dictionary['addr'][1]] = "true"
                    new_iDo_count = new_iDo_count + 1
                    new_iDo_addr = dictionary['addr'][0]

    if dictionary['event'][1] == "GAP_DeviceInitDone":
        hci_cmd_rsp_cmd = "fe00"
        if dictionary['status'][1] == "00":
            hci_cmd_rsp = "done"
        else:
            hci_cmd_rsp = "fail"

    if dictionary['event'][1] == "GAP_DeviceDiscoveryDone":
        hci_cmd_rsp_cmd = "fe05"
        discovery_done = 1
        hci_cmd_rsp = "done"

    if dictionary['event'][1] == "GAP_EstablishLink":
        hci_cmd_rsp_cmd = "fe09"
        if dictionary['status'][1] == "00":
            hci_cmd_rsp = "done"
            new_iDo_conn_handle = dictionary['conn_handle'][0]
        else:
            hci_cmd_rsp = "fail"

    if dictionary['event'][1] == "ATT_ReadRsp":
        hci_cmd_rsp_cmd = "fd8a"
        if dictionary['status'][1] == "00":
            hci_cmd_rsp = "done"
        else:
            hci_cmd_rsp = "fail"
        log("read value: " + dictionary['value'][1])
        read_rsp_list = list(dictionary['value'][0])

    if dictionary['event'][1] == "GAP_LinkTerminated":
        hci_cmd_rsp_cmd = "fe0a"
        if dictionary['status'][1] == "00":
            hci_cmd_rsp = "done"
        else:
            hci_cmd_rsp = "fail"

def BLESerialGetCodeDesc(cmd):
    if cmd == "fe00":
        return "GAP_DeviceInit"
    elif cmd == "fe04":
        return "GATT_DeviceDiscoveryRequest"
    elif cmd == "fe05":
        return "GATT_DeviceDiscoveryCancel"
    elif cmd == "fe09":
        return "GATT_EstablishLinkRequest"
    elif cmd == "fd8a":
        return "GATT_ReadCharValue"
    elif cmd == "fe0a":
        return "GATT_TerminateLinkRequest"

def BLESerialCmdWaitTime(cmd):
    if cmd == "fe00":
        return 5
    elif cmd == "fe05":
        return 5
    elif cmd == "fe09":
        return 15
    elif cmd == "fd8a":
        return 15
    elif cmd == "fe0a":
        return 15

def BLESerialCmd(cmd, skip_cmd, skip_rsp):
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

    if skip_rsp == 1:
        return 0

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
    print msg

def TriggerTask():
    global taskTrigger
    taskTrigger = 1

def Task(i):
    global taskStopSignal
    global taskTrigger
    global frame
    global bleDongle
    global ble_builder
    global ble_parser
    global new_iDo_count
    global discovery_done

    while True:
        # Wait 
        while taskTrigger == 0:
            time.sleep(0.1)
            if taskStopSignal == 1:
                break

        taskTrigger = 0

        if taskStopSignal == 1:
            log("Task exiting...")
            ble_parser.stop()
            break

        log("\nNew task")

        # Start discovery
        new_iDo_count = 0
        discovery_done = 0
        ble_builder.send("fe04", mode="\x03")
        if BLESerialCmd("fe04", 1, 1) != 0:
            log("Failed to start discovery")
            frame.TaskEndUpdateStatus(1, "蓝牙Dongle故障，请关闭程序/重新插拔Dongle后再试！")
        else:
            while discovery_done == 0 and new_iDo_count == 0:
                time.sleep(0.1)

            if new_iDo_count > 0:
                # Cancel discovery now
                if discovery_done == 0:
                    if BLESerialCmd("fe05", 0, 0) != 0:
                        log("Failed to cancel discovery")
                        frame.TaskEndUpdateStatus(1, "蓝牙Dongle故障，请关闭程序/重新插拔Dongle后再试！")

            if discovery_done == 0:
                log("Bug!")

            if new_iDo_count == 1:
                # Result looks good
                ble_builder.send("fe09", peer_addr = new_iDo_addr)
                if BLESerialCmd("fe09", 1, 0) != 0:
                    log("Failed to connect to device")
                    frame.TaskEndUpdateStatus(1, "测试失败！无法连上设备")
                    continue
                else:
                    log("BLE device connected.")
                    time.sleep(1)
                    ble_builder.send("fd8a", conn_handle = new_iDo_conn_handle, handle = "\x22\x00")
                    if BLESerialCmd("fd8a", 1, 0) != 0:
                        log("Failed to enable notification")
                        frame.TaskEndUpdateStatus(1, "测试失败！无法读取版本号")
                        continue
                    else:
                        log("got version data")
                        if read_rsp_list[0] == '\x31' and read_rsp_list[1] == '\x2e' and read_rsp_list[2] == '\x30' and read_rsp_list[3] == '\x2e' and read_rsp_list[4] == '\x30' and read_rsp_list[5] == '\x28' and read_rsp_list[6] == '\x30' and read_rsp_list[7] == '\x32' and read_rsp_list[8] == '\x41' and read_rsp_list[9] == '\x29' and read_rsp_list[10] == '\x00': 
                            log("TRUE")
                        else:
                            BLESerialCmd("fe0a", 0, 0)
                            frame.TaskEndUpdateStatus(1, "测试失败，版本检查失败")
                            continue

                        if BLESerialCmd("fe0a", 0, 0) != 0:
                            log("Failed to terminate BLE link")
                        
                        frame.TaskEndUpdateStatus(0, "测试通过，请试下一个")
            elif new_iDo_count > 1:
                log("More than one device discovered?")
                frame.TaskEndUpdateStatus(1, "测试失败，请检查是否有\n多余产品干扰？")
            else:
                frame.TaskEndUpdateStatus(1, "测试失败，未发现产品")

class MainFrame(wx.Frame):
    serialPort = None
    taskInProgress = 0
    task = None
    
    def __init__(self, title="纽扣电池测试工具v0.1", size=[400, 400]):
        global bleDongle
        global ble_builder
        global ble_parser

        wx.Frame.__init__(self,parent=None, style=wx.CAPTION | wx.SYSTEM_MENU | wx.CLOSE_BOX, title=title, size=size) 
        
        self.Bind(wx.EVT_CLOSE, self.OnClose)
        self.SetBackgroundColour((110, 110, 110))

        panel = wx.Panel(self, -1)  
        self.button = wx.Button(panel, -1, "               电池就位，开始检测               ", size=(300, 60), pos=(50, 250))  
        font = wx.Font(18, wx.DEFAULT, wx.NORMAL, wx.FONTWEIGHT_BOLD)
        self.button.SetFont(font)
        self.txtMsg = wx.StaticText(panel, -1, "", pos=(30, 50))
        self.txtMsg.SetFont(font)
        self.Bind(wx.EVT_BUTTON, self.OnTaskAction, self.button)  
        self.button.SetDefault() 
        self.taskInProgress = 0
    
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
            bleDongle = serial.Serial(port=dcom, baudrate=115200)
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

        # Initialize BLE Dongle
        ble_builder = BLEBuilder(bleDongle)
        ble_parser = BLEParser(bleDongle, callback=analyse_packet)

        #initialise the device
        if BLESerialCmd("fe00", 0, 0) != 0:
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
    
    def OnTaskAction(self, event):
        if self.taskInProgress == 0:
            self.button.Disable()
            self.taskInProgress = 1
            self.txtMsg.SetLabel("扫描设备...")
            self.txtMsg.SetForegroundColour((0, 0, 0))
            TriggerTask()

    def TaskEndUpdateStatus(self, status, msg):
        self.txtMsg.SetLabel(msg)
        if status == 1:
            self.txtMsg.SetForegroundColour((255, 0, 0))
        else:
            self.txtMsg.SetForegroundColour((0, 255, 0))
        self.button.Enable()
        self.taskInProgress = 0

if __name__ == '__main__':
    if not os.path.exists(logPath):
        os.makedirs(logPath)

    fn = "coin_battery-" + time.strftime('%Y-%m-%d_%H_%M_%S',time.localtime(time.time())) + ".log"
    logFd = open(logPath + "/" + fn, "w")
    if logFd == None:
        print "Failed to open " + fn + " for write log"
        sys.exit()

    app = wx.PySimpleApp()
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
