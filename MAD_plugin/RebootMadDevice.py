import mapadroid.utils.pluginBase
from functools import update_wrapper, wraps
from flask import render_template, Blueprint, jsonify
from mapadroid.madmin.functions import auth_required
from mapadroid.mitm_receiver.MITMReceiver import MITMReceiver
from threading import Thread
import socket
import _thread
import os
import sys
import time
import datetime
import json
import pickle
import requests


class RebootMadDevice(mapadroid.utils.pluginBase.Plugin):
    """This plugin is just the identity function: it returns the argument
    """

    def __init__(self, mad):
        super().__init__(mad)

        self._rootdir = os.path.dirname(os.path.abspath(__file__))

        self._mad = mad

        self._pluginconfig.read(self._rootdir + "/plugin.ini")
        self._versionconfig.read(self._rootdir + "/version.mpl")
        self.author = self._versionconfig.get("plugin", "author", fallback="unknown")
        self.url = self._versionconfig.get("plugin", "url", fallback="https://www.maddev.eu")
        self.description = self._versionconfig.get("plugin", "description", fallback="unknown")
        self.version = self._versionconfig.get("plugin", "version", fallback="unknown")
        self.pluginname = self._versionconfig.get("plugin", "pluginname", fallback="https://www.maddev.eu")
        self.staticpath = self._rootdir + "/static/"
        self.templatepath = self._rootdir + "/template/"

        self._routes = [
            ("/rmdstatus", self.rmdstatus),
            ("/rmdstatuspage", self.rmdstatus_route),
            ("/rmdreadme", self.rmdreadme_route),
        ]

        self._hotlink = [
            ("Plugin Status Page", "/rmdstatuspage", "RMD - Status Page"),
            ("Plugin Readme", "/rmdreadme", "RMD - Readme Page"),
        ]

        if self._pluginconfig.getboolean("plugin", "active", fallback=False):
            self._plugin = Blueprint(str(self.pluginname), __name__, static_folder=self.staticpath,
                                     template_folder=self.templatepath)

            for route, view_func in self._routes:
                self._plugin.add_url_rule(route, route.replace("/", ""), view_func=view_func)

            for name, link, description in self._hotlink:
                self._mad['madmin'].add_plugin_hotlink(name, self._plugin.name + "." + link.replace("/", ""),
                                                       self.pluginname, self.description, self.author, self.url,
                                                       description, self.version)

    def perform_operation(self):
        """The actual implementation of the identity plugin is to just return the
        argument
        """

        # do not change this part ▽▽▽▽▽▽▽▽▽▽▽▽▽▽▽
        if not self._pluginconfig.getboolean("plugin", "active", fallback=False):
            return False
        self._mad['madmin'].register_plugin(self._plugin)
        # do not change this part △△△△△△△△△△△△△△△

        # dont start plugin in config mode
        if self._mad['args'].config_mode == True:
            self._mad['logger'].info("Plugin - RebootMadDevice not aktive while configmode")
            return False

        # read config parameter
        self._reboothistory: dict = {}
        self._webhookhistory: dict = {}
        self._clienthistory: dict = {}
        self._device_status: dict = {}
        self._firststart = True
        self._last_client_connect = None

        self._token = self._pluginconfig.get("auth", "token", fallback=None)
        self._try_adb_first = self._pluginconfig.get("rebootoptions", "try_adb_first", fallback='no')
        self._mitm_timeout = self._pluginconfig.get("rebootoptions", "mitm_timeout", fallback=15)
        self._proto_timeout = self._pluginconfig.get("rebootoptions", "proto_timeout", fallback=15)
        self._force_reboot_timeout = self._pluginconfig.get("rebootoptions", "force_reboot_timeout", fallback=20)
        self._reboot_waittime = self._pluginconfig.get("rebootoptions", "reboot_waittime", fallback=20)
        self._host = self._pluginconfig.get("socketserver", "host", fallback=None)
        self._port = self._pluginconfig.get("socketserver", "port", fallback=None)
        self._webhook_enable = self._pluginconfig.get("discord", "webhook_enable", fallback=None)
        self._webhookurl = self._pluginconfig.get("discord", "webhookurl", fallback=None)

        self.rmdThread()
        self.rmdserverThread()

        return True

    def rmdThread(self):
        rmd_worker = Thread(name="RebootMadDevice", target=self.rmdStatusChecker)
        rmd_worker.daemon = True
        rmd_worker.start()

    def rmdserverThread(self):
        rmd_worker = Thread(name="RebootMadDevice", target=self.rmdSocketServer)
        rmd_worker.daemon = True
        rmd_worker.start()

    def makeTimestamp(self):
        ts = int(time.time())
        return ts

    def calc_past_min_from_now(self, timestamp):
        """ calculate time between now and given timestamp """
        now = datetime.datetime.now()
        if timestamp == None or timestamp == "":
            return 99999
        elif datetime.datetime.fromtimestamp(timestamp) > now:
            return 0
        diffToNow = now - datetime.datetime.fromtimestamp(timestamp)
        past_min_from_now = int(diffToNow.seconds / 60)
        return int(past_min_from_now)

    def calc_past_sec_from_now(self, timestamp):
        """ calculate time between now and given timestamp """
        now = datetime.datetime.now()
        if timestamp == None or timestamp == "":
            return None
        elif datetime.datetime.fromtimestamp(timestamp) > now:
            return 0
        diffToNow = now - datetime.datetime.fromtimestamp(timestamp)
        past_sec_from_now = int(diffToNow.seconds)
        return int(past_sec_from_now)
		
    def makeTimestampReadable(self, timestamp):
        if timestamp is None:
            return "None"
        else:
            rts = datetime.datetime.fromtimestamp(timestamp)
            return rts

    def sec2time(self, sec):
        if sec is None:
            return "None"
        # easy way to get mm:ss 
        return "%02d:%02d" % divmod(sec, 60) 

    	   
    def rmdStatusChecker(self):

        while True:
            madmin_stats = self._mad['db_wrapper'].download_status()
            mitm_stats = json.loads(self._mad['mitm_receiver_process'].status(None, None))
            self._mad['logger'].debug('rmdStatusChecker: ' + str(madmin_stats))
            self._mad['logger'].debug('rmdStatusChecker: ' + str(mitm_stats))

            if not self._firststart:
                for device in madmin_stats:

                    # get all values from mad
                    device_origin = device['name']
                    worker_status = device['rmname']
                    worker_mode = device['mode']
                    injection_status = mitm_stats['origin_status'][device_origin]['injection_status']
                    last_mitm_data = mitm_stats['origin_status'][device_origin]['latest_data']
                    last_proto_data = device['lastProtoDateTime']
                    sleep_time = device['currentSleepTime']
                    data_plus_sleep = last_proto_data or 0 + sleep_time
                    try:
                        last_reboot_time = self._reboothistory[device_origin]['last_reboot_time']
                        reboot_count = self._reboothistory[device_origin]['reboot_count']
                    except:
                        last_reboot_time = None
                        reboot_count = 0
                        no_data = 0
                        self._reboothistory[device_origin] = {'last_reboot_time': None, 'reboot_count': 0, 'no_data': 0 }
                    last_client_connect = self._clienthistory.get(device_origin, None)
                    try:
                        webhook_id = self._webhookhistory[device_origin]['webhook_id']
                    except:
                        self._webhookhistory[device_origin] = {'reboot_type': None,'force_option': None,'webhook_id': None}

                    self._mad['logger'].debug('rmdStatusChecker - device: ' + str(device_origin))
                    self._mad['logger'].debug('rmdStatusChecker - timestamp: ' + str(self.makeTimestamp()))
                    self._mad['logger'].debug('rmdStatusChecker - last_mitm_data: ' + str(last_mitm_data))
                    self._mad['logger'].debug('rmdStatusChecker - sleep_time: ' + str(sleep_time))
                    self._mad['logger'].debug('rmdStatusChecker - data_plus_sleep: ' + str(data_plus_sleep))
                    self._mad['logger'].debug('rmdStatusChecker - minutes_last_mitm_data: ' + str(self.calc_past_min_from_now(last_mitm_data)))
                    self._mad['logger'].debug('rmdStatusChecker - minutes_last_data_plus_sleep: ' + str(self.calc_past_min_from_now(data_plus_sleep)))

                    # check if reboot is nessessary

                    if self._try_adb_first == 'no':
                        if (injection_status == False and self.calc_past_min_from_now(last_mitm_data) > int(self._mitm_timeout)) or \
                                self.calc_past_min_from_now(data_plus_sleep) > int(self._proto_timeout):
                            self._mad['logger'].debug('rmdStatusChecker - reboot nessessary: yes')					
                            reboot_nessessary = 'yes'
                            reboot_force = 'yes'
                            if self.calc_past_min_from_now(last_reboot_time) < int(self._reboot_waittime):
                                self._mad['logger'].debug('rmdStatusChecker - reboot nessessary: still rebooting')
                                reboot_nessessary = 'rebooting'
                        else:
                            reboot_force = 'no'
                            reboot_nessessary = 'no'
                            reboot_count = 0
                            # clear webhook_id after fixed message
                            if self._webhookhistory[device_origin]['webhook_id'] is not None:
                                self.discord_message(device_origin, fixed=True)
                                self._webhookhistory[device_origin] = {'reboot_type': None,'force_option': None,'webhook_id': None}
                    else:
                        if (injection_status == False and self.calc_past_min_from_now(last_mitm_data) > int(self._mitm_timeout)) or \
                                self.calc_past_min_from_now(data_plus_sleep) > int(self._proto_timeout):
                            self._mad['logger'].debug('rmdStatusChecker - reboot nessessary: yes')					
                            reboot_nessessary = 'yes'
                            reboot_force = 'no'
                            if self.calc_past_min_from_now(last_reboot_time) < int(self._reboot_waittime):
                                self._mad['logger'].debug('rmdStatusChecker - reboot nessessary: still rebooting')
                                reboot_nessessary = 'rebooting'
                            if self.calc_past_min_from_now(last_mitm_data) > int(self._force_reboot_timeout) or \
                                    self.calc_past_min_from_now(data_plus_sleep) > int(self._force_reboot_timeout):
                                self._mad['logger'].debug('rmdStatusChecker - force reboot: yes')
                                reboot_force = 'yes'
                        else:
                            reboot_force = 'no'
                            reboot_nessessary = 'no'
                            reboot_count = 0
                            # clear webhook_id after fixed message
                            if self._webhookhistory[device_origin]['webhook_id'] is not None:
                                self.discord_message(device_origin, fixed=True)
                                self._webhookhistory[device_origin] = {'reboot_type': None,'force_option': None,'webhook_id': None}

                    # save all values to device_status
                    self._device_status[device_origin] = {'injection_status': injection_status,
                                                          'worker_status': worker_status,
                                                          'worker_mode': worker_mode,
                                                          'last_mitm_data': last_mitm_data,
                                                          'last_proto_data': last_proto_data,
                                                          'last_reboot_time': last_reboot_time,
                                                          'reboot_count': reboot_count,
                                                          'reboot_nessessary': reboot_nessessary,
                                                          'reboot_force': reboot_force,
                                                          'last_client_connect': last_client_connect}

                    # Update no_data time and existing Discord messages
                    if self._reboothistory[device_origin]['no_data'] < self.calc_past_min_from_now(self._device_status[device_origin]['last_mitm_data']):
                        self._reboothistory[device_origin]['no_data'] = self.calc_past_min_from_now(self._device_status[device_origin]['last_mitm_data'])
                        if self._webhookhistory[device_origin]['webhook_id'] is not None:
                            self._mad['logger'].info('rmdStatusChecker: update Discord message')
                            self.discord_message(device_origin)

                self._mad['logger'].debug('rmdStatusChecker: ' + str(self._device_status))

            # Go sleep until next check
            self._firststart = False
            time.sleep(int(self._pluginconfig.get("rebootoptions", "sleeptime_between_check", fallback=5)) * 60)

    def on_new_client(self, clientsocket, addr):
        self._mad['logger'].info('rmdserver: Got connection from ' + str(addr))

        # receive token for auth
        if clientsocket.recv(8192).decode().replace("\r\n", "") == self._token:
            self._mad['logger'].debug('rmdclient: auth token successfull')

            # receive data from client
            device_origin = clientsocket.recv(8192).decode().replace("\r\n", "")
            self._mad['logger'].info('rmdclient: ' + str(addr) + ' request data from device >> ' + device_origin)

            try:
                # get data to send
                data = self._device_status[device_origin]
                # set timestamp for client connect
                self._device_status[device_origin]['last_client_connect'] = self.makeTimestamp()
                self._clienthistory[device_origin] = self.makeTimestamp()
                # send data to client
                try:
                    clientsocket.send(pickle.dumps(data))
                    if self._device_status[device_origin]['reboot_nessessary'] == 'yes':
                        self._device_status[device_origin]['last_reboot_time'] = self.makeTimestamp()
                        self._reboothistory[device_origin]['last_reboot_time'] = self.makeTimestamp()
                        self._reboothistory[device_origin]['reboot_count'] = self._device_status[device_origin]['reboot_count'] + 1
                        self._reboothistory[device_origin]['no_data'] = self.calc_past_min_from_now(self._device_status[device_origin]['last_mitm_data'])
                        self._device_status[device_origin]['reboot_nessessary'] = 'rebooting'
                        self._mad['logger'].debug(
                            'rmdserver: data send to client ' + str(self._device_status[device_origin]))
                except:
                    self._mad['logger'].error('rmdserver: error sending data to client')

                # get returncode from client
                try:
                    returncode = clientsocket.recv(8192).decode().replace("\r\n", "")
                    self._mad['logger'].info('rmdclient: got reboot returncode from client: ' + str(returncode))
                except:
                    self._mad['logger'].error('rmdserver: error receiving returncode from reboot')
                try:
                    if int(returncode) > 0 and self._webhook_enable == 'yes':
                        self._mad['logger'].info('rmdserver: create webhook with returncode ' + str(returncode))
                        try:
                            self.create_webhook_data(device_origin, returncode)
                        except:
                            self._mad['logger'].error('rmdserver: error creating webhookdatahistory')
                        try:
                            self.discord_message(device_origin)
                        except:
                            self._mad['logger'].error('rmdserver: error sending webhook message')
                except:
                    self._mad['logger'].error('rmdserver: error with webhook')

            except KeyError:
                self._mad['logger'].error('rmdclient: unknown origin')
        else:
            self._mad['logger'].info('rmdclient: auth token wrong')
        clientsocket.close()

    def rmdSocketServer(self):
        time.sleep(360)
        s = socket.socket()  # Create a socket object
        host = self._host  # Get local machine name
        port = int(self._port)  # Reserve a port for your service.

        self._mad['logger'].info('rmdserver: SocketServer started on ' + host + ' with port ' + str(port) + ' !!')
        self._mad['logger'].info('rmdserver: Waiting for clients...')

        s.bind((host, port))  # Bind to the port
        s.listen(5)  # Now wait for client connection.

        while True:
            c, addr = s.accept()  # Establish connection with client.
            _thread.start_new_thread(self.on_new_client, (c, addr))
        s.close()

    def create_webhook_data(self, device_origin, returncode):
        # decode returncode for information
        # EXIT Code 100 = Reboot via adb
        # EXIT Code 200 = Reboot via HTML
        # EXIT Code 300 = Reboot via GPIO
        # EXIT Code 400 = Reboot via i2c
        # EXIT Code 500 = Reboot via cmd
        # EXIT Code 600 = Reboot via PB
        # EXIT Code 700 = Reboot via POE	
        # EXIT Code +50 = force Option
        if returncode == '100':
            reboot_type = 'ADB'
            force_option = 'no'
        elif returncode == '200':
            reboot_type = 'HTML'
            force_option = 'no'
        elif returncode == '250':
            reboot_type = 'HTML'
            force_option = 'yes'
        elif returncode == '300':
            reboot_type = 'GPIO'
            force_option = 'no'
        elif returncode == '350':
            reboot_type = 'GPIO'
            force_option = 'yes'
        elif returncode == '400':
            reboot_type = 'I2C'
            force_option = 'no'
        elif returncode == '450':
            reboot_type = 'I2C'
            force_option = 'yes'
        elif returncode == '500':
            reboot_type = 'CMD'
            force_option = 'no'
        elif returncode == '550':
            reboot_type = 'CMD'
            force_option = 'yes'
        elif returncode == '600':
            reboot_type = 'PB'
            force_option = 'no'
        elif returncode == '650':
            reboot_type = 'PB'
            force_option = 'yes'
        elif returncode == '700':
            reboot_type = 'POE'
            force_option = 'no'
        elif returncode == '750':
            reboot_type = 'POE'
            force_option = 'yes'
        elif returncode == '800':
            reboot_type = 'SNMP'
            force_option = 'no'
        elif returncode == '850':
            reboot_type = 'SNMP'
            force_option = 'yes'

        self._webhookhistory[device_origin]['reboot_type'] = reboot_type
        self._webhookhistory[device_origin]['force_option'] = force_option
        self._mad['logger'].debug('rmdserver: webhookdata created in webhookhistory with:')
        self._mad['logger'].debug(self._webhookhistory[device_origin])

    def discord_message(self, device_origin, fixed=False):
        # create data for webhook
        self._mad['logger'].info('Start Webhook for device ' + device_origin )

        now = datetime.datetime.utcnow()
        data = {
          "content": "",
          "username": "Alert!",
          "avatar_url": "https://github.com/GhostTalker/icons/blob/main/rmd/messagebox_critical_256.png?raw=true",
          "embeds": [
            {
              "title": "Device restarted!", 
              "color": 16711680,
              "author": {
                "name": "RebootMadDevice",
                "url": "https://github.com/GhostTalker/RebootMadDevice",
                "icon_url": "https://github.com/GhostTalker/icons/blob/main/Ghost/GhostTalker.jpg?raw=true"
              },
               "thumbnail": {
                   "url": "https://github.com/GhostTalker/icons/blob/main/rmd/reboot.jpg?raw=true"
               },
               "fields": [
                {
                  "name": "Device",
                  "value": device_origin,
                  "inline": "true"
                },
                {
                  "name": "Reboot",
                  "value": self._webhookhistory[device_origin]['reboot_type'],
                  "inline": "true"
                },
                {
                  "name": "Force",
                  "value": self._webhookhistory[device_origin]['force_option'],
                  "inline": "true"
                }
              ]
            }
          ]
        }
        # add timestamp
        data["embeds"][0]["timestamp"] = str(now)

        # send webhook
        self._mad['logger'].debug('rmdserver: data to send with webhook:')
        self._mad['logger'].debug(data)
        self._mad['logger'].debug(self._webhookhistory[device_origin]['webhook_id'])

        if self._webhookhistory[device_origin]['webhook_id'] is None:
            data["embeds"][0]["description"] = f"`{device_origin}` did not send useful data for more than `{self._reboothistory[device_origin]['no_data']}` minutes!\nReboot count: `{self._reboothistory[device_origin]['reboot_count']}`"
            try:
                result = requests.post(self._webhookurl, json = data, params={"wait": True})
                result.raise_for_status()
                answer = result.json()
                self._mad['logger'].debug(answer)
                self._webhookhistory[device_origin]["webhook_id"] = answer["id"]
                self._mad['logger'].debug(self._webhookhistory[device_origin]["webhook_id"])
            except requests.exceptions.RequestException as err:
                self._mad['logger'].info(err)
        else:
            self._mad['logger'].debug('parameter fixed is: ' + str(fixed))
            if not fixed:
                data["embeds"][0]["description"] = f"`{device_origin}` did not send useful data for more than `{self._reboothistory[device_origin]['no_data']}` minutes!\nReboot count: `{self._reboothistory[device_origin]['reboot_count']}`\nFixed :x:"
            else:
                data["embeds"][0]["description"] = f"`{device_origin}` did not send useful data for more than `{self._reboothistory[device_origin]['no_data']}` minutes!\nReboot count: `{self._reboothistory[device_origin]['reboot_count']}`\nFixed :white_check_mark:"

            try:
                result = requests.patch(self._webhookurl + "/messages/" + self._webhookhistory[device_origin]["webhook_id"], json = data)
                result.raise_for_status()
            except requests.exceptions.RequestException as err:
                self._mad['logger'].info(err)

        return result.status_code


    @auth_required
    def rmdstatus(self):
        #device_status_list = list(self._device_status.items())
        device_status_list = []
        for device_origin, value in self._device_status.items():		
            listitem = {}
            listitem["device_origin"] = device_origin
            listitem["injection_status"] = self._device_status[device_origin]["injection_status"]
            listitem["worker_mode"] = self._device_status[device_origin]["worker_mode"]
            listitem['worker_status'] = self._device_status[device_origin]["worker_status"]
            listitem["last_mitm_data"] = self.sec2time(self.calc_past_sec_from_now(self._device_status[device_origin]["last_mitm_data"]))
            listitem["last_proto_data"] = self.sec2time(self.calc_past_sec_from_now(self._device_status[device_origin]["last_proto_data"]))
            listitem["reboot_nessessary"] = self._device_status[device_origin]["reboot_nessessary"]
            listitem["reboot_force"] = self._device_status[device_origin]["reboot_force"]
            listitem["last_reboot_time"] = self.sec2time(self.calc_past_sec_from_now(self._device_status[device_origin]["last_reboot_time"]))
            listitem["reboot_count"] = self._device_status[device_origin]["reboot_count"]
            listitem["last_client_connect"] = self.sec2time(self.calc_past_sec_from_now(self._device_status[device_origin]["last_client_connect"]))
            device_status_list.append(listitem)
        return jsonify(device_status_list)

    @auth_required
    def rmdstatus_route(self):
        return render_template('rmdstatus.html', title="RMD Status",
                               time=self._mad['args'].madmin_time,
                               responsive=str(self._mad['args'].madmin_noresponsive).lower())

    @auth_required
    def rmdreadme_route(self):
        return render_template("rmdreadme.html",
                               header="RebootMadDevice Readme", title="RebootMadDevice Readme"
                               )


