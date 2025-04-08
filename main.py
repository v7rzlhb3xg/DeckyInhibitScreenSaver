import decky_plugin
import asyncio
import queue
from settings import SettingsManager

def import_third_party_lib():
    import sys
    from pathlib import Path
    plugin_dir = Path(__file__).parent.resolve()
    decky_plugin.logger.info(f'plugin dir: {plugin_dir}')
    sys.path.insert(0, str(plugin_dir))
    sys.path.insert(0, str(plugin_dir.joinpath("lib")))

def setup_environ_vars():
    import os
    os.environ['XDG_RUNTIME_DIR'] = '/run/user/1000'
    os.environ['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=/run/user/1000/bus'
    os.environ['HOME'] = '/home/deck'

import_third_party_lib()
setup_environ_vars()
settings_dir = decky_plugin.DECKY_PLUGIN_SETTINGS_DIR
settings = SettingsManager(name="settings", settings_directory=settings_dir)
event_queue = queue.Queue()

from dbus_next.aio import MessageBus, ProxyInterface
from dbus_next import Message, MessageType, Variant
from dbus_next.service import ServiceInterface, method

bus = None

class AppRequest:
    def __init__(self, sender, cookie, application, reason):
        self.sender = sender
        self.cookie = cookie
        self.application = application
        self.reason = reason

    async def is_connected(self):
        global bus
        message = Message(
            destination='org.freedesktop.DBus',
            path='/org/freedesktop/DBus',
            interface='org.freedesktop.DBus',
            member='GetConnectionUnixProcessID',
            signature='s',
            body=[self.sender]
        )
        reply = await bus.call(message)
        return reply.message_type != MessageType.ERROR

class BaseInterface(ServiceInterface):
    ignore_application = ["Steam", "./steamwebhelper"]
    request_map = {}
    cookie = 0

    def __init__(self, service):
        super().__init__(service)

    @method()
    async def Inhibit(self, application: 's', reason: 's') -> 'u':
        if application in BaseInterface.ignore_application: return 0
        decky_plugin.logger.info(f'called Inhibit with application={application} and reason={reason}')
        event_queue.put({"type": "Inhibit"})
        sender = ServiceInterface.last_msg.sender
        BaseInterface.cookie += 1
        BaseInterface.request_map[BaseInterface.cookie] = AppRequest(sender, BaseInterface.cookie, application, reason)
        return BaseInterface.cookie

    @method()
    def UnInhibit(self, cookie: 'u'):
        if cookie == 0: return
        decky_plugin.logger.info(f'called UnInhibit with cookie={cookie}')
        if BaseInterface.request_map.pop(cookie, None) is None:
            decky_plugin.logger.info(f'cannot find cookie={cookie}')
        if len(BaseInterface.request_map) == 0:
            event_queue.put({"type": "UnInhibit"})

class InhibitInterface(BaseInterface):
    def __init__(self):
        super().__init__('org.freedesktop.ScreenSaver')

class PMInhibitInterface(BaseInterface):
    def __init__(self):
        super().__init__('org.freedesktop.PowerManagement.Inhibit')

async def stop_dbus():
    global bus
    try:
        if bus is not None:
            bus.disconnect()
        bus = None
    except Exception as e:
        decky_plugin.logger.info(f"error: {e}")

async def start_dbus():
    global bus
    await stop_dbus()
    try:
        bus = await MessageBus().connect()
        interface = InhibitInterface()
        pm_interface = PMInhibitInterface()
        bus.export('/ScreenSaver', interface)
        bus.export('/org/freedesktop/ScreenSaver', interface)
        bus.export('/org/freedesktop/PowerManagement/Inhibit', pm_interface)
        await bus.request_name('org.freedesktop.PowerManagement')
        await bus.request_name('org.freedesktop.ScreenSaver')
    except Exception as e:
        decky_plugin.logger.info(f"error: {e}")

async def poll_mpris():
    global bus
    inhibit_cookie = None
    while True:
        try:
            mpris_services = [n for n in (await bus.introspect('org.freedesktop.DBus', '/org/freedesktop/DBus'))
                              .interfaces['org.freedesktop.DBus'].properties['Names'].value
                              if n.startswith('org.mpris.MediaPlayer2.')]
            playing = False
            for svc in mpris_services:
                try:
                    player_obj = await bus.get_proxy_object(svc, '/org/mpris/MediaPlayer2', None)
                    props = player_obj.get_interface('org.freedesktop.DBus.Properties')
                    status = await props.call_get('org.mpris.MediaPlayer2.Player', 'PlaybackStatus')
                    if status.value == 'Playing':
                        playing = True
                        break
                except Exception:
                    continue
            if playing and not inhibit_cookie:
                message = Message(
                    destination='org.freedesktop.ScreenSaver',
                    path='/org/freedesktop/ScreenSaver',
                    interface='org.freedesktop.ScreenSaver',
                    member='Inhibit',
                    signature='ss',
                    body=['Cider-MPRIS', 'MPRIS Playback']
                )
                reply = await bus.call(message)
                if reply.message_type == MessageType.METHOD_RETURN:
                    inhibit_cookie = reply.body[0]
            elif not playing and inhibit_cookie:
                message = Message(
                    destination='org.freedesktop.ScreenSaver',
                    path='/org/freedesktop/ScreenSaver',
                    interface='org.freedesktop.ScreenSaver',
                    member='UnInhibit',
                    signature='u',
                    body=[inhibit_cookie]
                )
                await bus.call(message)
                inhibit_cookie = None
        except Exception as e:
            decky_plugin.logger.info(f"MPRIS poll error: {e}")
        await asyncio.sleep(5)

class Plugin:

    async def start_backend(self):
        decky_plugin.logger.info("Start backend server")
        await start_dbus()
        asyncio.create_task(poll_mpris())

    async def stop_backend(self):
        decky_plugin.logger.info("Stop backend server")
        await stop_dbus()
        event_queue.queue.clear()

    async def is_running(self):
        global bus
        return bus is not None

    async def get_event(self):
        global bus
        if bus is None:
            return []
        res = []
        while not event_queue.empty():
            try:
                res.append(event_queue.get_nowait())
            except queue.Empty:
                continue
        if len(res) > 0:
            return res
        cookies = list(BaseInterface.request_map.keys())
        clear = False
        for c in cookies:
            connected = await BaseInterface.request_map[c].is_connected()
            if not connected:
                BaseInterface.request_map.pop(c)
                clear = True
        if clear and len(BaseInterface.request_map) == 0:
            return [{"type": "UnInhibit"}]
        return []

    async def get_settings(self, key: str, defaults):
        decky_plugin.logger.info('[settings] get {}'.format(key))
        return settings.getSetting(key, defaults)

    async def set_settings(self, key: str, value):
        decky_plugin.logger.info('[settings] set {}: {}'.format(key, value))
        return settings.setSetting(key, value)

    async def _main(self):
        decky_plugin.logger.info("Hello World!")

    async def _unload(self):
        decky_plugin.logger.info("Goodnight World!")
        await stop_dbus()

    async def _uninstall(self):
        pass

    async def _migration(self):
        pass
