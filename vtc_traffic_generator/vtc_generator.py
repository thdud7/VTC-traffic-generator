import ffmpeg
import subprocess
import pulsectl
import os
import random
import signal
import sys
import time
import json
import socketserver
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCServer
from pathlib import PurePath
import asyncio
from playwright.async_api import async_playwright
import concurrent.futures
import threading

def run_controller():
    num_clients = len(config['vtc_clients'])
    vtc_clients = []

    # Instantiate clients
    for x in range(num_clients):

        # Create VTC client
        vtc_clients.append(VtcClient(config['vtc_clients'][x][0], config['vtc_clients'][x][1]))

    # Threaded VTC client initialization
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(vtc_clients))
    executor.map(VtcClient.initialize_client, vtc_clients)
    executor.shutdown(wait=False)

    # Begin client dialog
    # Randomly select VTC_client as long as it wasn't the last one picked.
    chosen_client = None
    candidate_client = random.choice(vtc_clients)

    # VTC conversation loop
    start_time = time.time()
    elapsed_time = 0

    # Converse for VTC duration
    while elapsed_time < config['duration'] * 60:
        while candidate_client is chosen_client:
            candidate_client = random.choice(vtc_clients)

        chosen_client = candidate_client
        uri = 'http://' + chosen_client.ip + ':' + str(chosen_client.port)

        # Print bot name on controller STDOUT for debugging / manual bot admittance
        with xmlrpc.client.ServerProxy(uri) as proxy:
            print(proxy.get_name() + " speaking now.")

        # Command selected VTC client to take a dialog cycle
        with xmlrpc.client.ServerProxy(uri) as proxy:
            dialog_complete = False
            dialog_complete = proxy.dialog_cycle()

        elapsed_time = time.time() - start_time

    # Tear down VTC
    print("VTC complete, closing session now.")
    for x in range(num_clients):
        uri = 'http://' + vtc_clients[x].ip + ':' + str(vtc_clients[x].port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.stop_video(vtc_clients[x].video_pid)

class AsyncXMLRPCServer(socketserver.ThreadingMixIn,SimpleXMLRPCServer): pass

class VtcClient:
    def __init__(self, ip='0.0.0.0', port=100):
        self.ip = ip
        self.port = int(port)
        self.video_pid = 0

    def initialize_client(self):
        uri = 'http://' + self.ip + ':' + str(self.port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.initialize_vtc_client()

        # Start client video stream to virtual camera device
        if config['videoconference']:
            with xmlrpc.client.ServerProxy(uri) as proxy:
                self.video_pid = proxy.play_video()

        # Connect to VTC session
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.run_connect(config['duration'])


def run_client(client_config):
    # Register functions and respond to calls indefinitely
    #server = SimpleXMLRPCServer(("0.0.0.0", client_config['c2_port']), allow_none=True)
    server = AsyncXMLRPCServer(("0.0.0.0", client_config['c2_port']), allow_none=True)
    print("Listening on port: " + str(client_config['c2_port']))

    server.register_function(initialize_vtc_client, "initialize_vtc_client")
    server.register_function(play_video, "play_video")
    server.register_function(stop_video, "stop_video")
    server.register_function(dialog_cycle, "dialog_cycle")
    server.register_function(get_name, "get_name")
    server.register_function(run_connect, "run_connect")
    server.register_function(stop_video, "stop_video")

    server.serve_forever()


# XMLRPC
# Initialize client
# Set pulse audio devices and device volume
# Check for v4l2 kernel mod
def initialize_vtc_client():
    print("Initializing VTC client")

    # Set up audio devices
    pulse = pulsectl.Pulse()
    sinks = pulse.sink_list()
    sources = pulse.source_list()

    if "name='virtual_speaker'" not in str(sinks) or "name='virtual_mic'" not in str(sources):
        print("Creating PulseAudio virtual devices")
        subprocess.run(
            'pactl load-module module-null-sink sink_name="virtual_speaker" '
            'sink_properties=device.description="virtual_speaker"',
            capture_output=True, shell=True)
        subprocess.run(
            'pactl load-module module-remap-source master="virtual_speaker.monitor" source_name="virtual_mic" '
            'source_properties=device.description="virtual_mic"',
            capture_output=True, shell=True)

    # Set volume levels and unmute devices
    for sink in sinks:
        pulse.volume_set_all_chans(sink, .8)
        pulse.mute(sink, False)

    for source in sources:
        pulse.volume_set_all_chans(source, .8)
        pulse.mute(source, False)

    # Check for v4l2 virtual webcam kernel module
    if 'v4l2loopback' not in str(subprocess.run(['lsmod'], capture_output=True)):
        print(
            "Error: v4l2loopback kernel module not loaded. Try: sudo modprobe v4l2loopback video_nr=5 exclusive_caps=1")

    print(config['bot_name'] + " configured.")


# XMLRPC
def dialog_cycle():
    print(config['bot_name'] + " speaking now.")

    # Calculate the number of sentences a VTC client speaks in a single turn
    # Left skewed, lower bound at 1, upper bound at number of sentences in a given conversation
    num_sentences = round(abs(random.gauss(0, 2))) + 1

    # Select conversation
    convo_root = str(PurePath(config['audio_path'], config['voice_name']))
    convo_list = os.listdir(convo_root)
    convo_index = random.randrange(0, len(convo_list))
    convo_path = str(PurePath(convo_root, convo_list[convo_index]))
    print("CONVOPATH:" + convo_path)

    # Select dialog lines and play them
    audio_filenames = os.listdir(convo_path)
    if num_sentences > len(audio_filenames):
        num_sentences = len(audio_filenames)

    print("Number of lines:" + str(num_sentences))

    index = 0
    while index < num_sentences:
        # Play file
        filename = str(index) + ".flac"
        audiofile_fullpath = str(PurePath(convo_path, filename))

        # Play audio
        play_audio(audiofile_fullpath)
        index += 1

    return True


# No XMLRPC needed, simply a local function on the remote VTC client
def play_audio(audio_file_path):
    subprocess.run('paplay -d virtual_speaker ' + audio_file_path, capture_output=True, shell=True)


# XMLRPC
def play_video():
    # Setup streaming from file to v4l2 device
    if config['videoconference']:
        try:
            video_filepath = str(PurePath(config['video_path'], config['video_name']))
            print(video_filepath)
            # time.sleep(5)

            if "270" in config['video_name']:
                process = (
                    ffmpeg
                        .input(video_filepath, re=None, stream_loop=-1)
                        .filter('format', 'yuv420p')
                        .drawtext(text=config['bot_name'], x='(w-text_w)/2', y='h-th-20', fontcolor='red', fontsize=50)
                        .output('/dev/video5', format='v4l2')
                )
            else:
                process = (
                    ffmpeg
                        .input(video_filepath, re=None, stream_loop=-1)
                        .filter('format', 'yuv420p')
                        .drawtext(text=config['bot_name'], x='(w-text_w)/2', y='h-th-50', fontcolor='red', fontsize=200)
                        .output('/dev/video5', format='v4l2')
                )

            # Launch video playback
            print("Launching video playback")
            # process = process.run_async(pipe_stdin=True, quiet=True)
            process = process.run_async(pipe_stdin=True)

        except ffmpeg.Error as e:
            print('stdout:', e.stdout.decode('utf8'))
            print('stderr:', e.stderr.decode('utf8'))
            raise e

        return process.pid

    else:
        return False


# XMLRPC
async def connect_vtc_session(duration):
    if config['vtc_platform'].lower() == "jitsi":
        print("Connecting to Jitsi VTC session")
        async with async_playwright() as p:
            # Consider pointing to local chromium, e.g. /usr/bin/google-chrome
            # browser = await p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"], headless=False)
            browser = await p.chromium.launch(args=["--use-fake-ui-for-media-stream"], headless=False)
            page = await browser.new_page(ignore_https_errors=True)
            await page.goto(config['vtc_url'])
            # await page.goto("https://localhost:8443/")
            print(await page.title())

            # Open microphone settings
            #await asyncio.sleep(3)
            #await page.click("[aria-label=\"Audio settings\"]")
            #await asyncio.sleep(3)
            #await page.click("li[role=\"radio\"]:has-text(\"virtual_mic\")")
            #await asyncio.sleep(2)
            #await page.click("[aria-label=\"Audio settings\"]")
            #await page.click("#largeVideo")

            '''
            # Open microphone settings
            await page.click("#new-toolbox div div div div >> :nth-match(svg, 2)")
            # Select virtual_mic
            await page.click("#new-toolbox div div div div >> :nth-match(div:has-text(\"virtual_mic\"), 5)")
            # Close mic selection dialog
            await page.click("#new-toolbox div div div div div >> :nth-match(svg, 2)")
            '''

            # Set participant duration
            # await page.pause()
            await asyncio.sleep(duration * 60)
            await browser.close()

    else:
        print("Missing connector to vtc platform: " + config['vtc_platform'])

    return(config['bot_name'] + " connected to VTC session.")

def run_connect(duration):
    x=threading.Thread(target=asyncio.run(connect_vtc_session((duration))))
    #asyncio.run(connect_vtc_session(duration))


# XMLRPC
def stop_video(video_pid):
    print("Stopping video")
    os.kill(video_pid, signal.SIGTERM)


# XMLRPC
def get_name():
    return config['bot_name']


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Error: must specify configuration JSON file.")
        exit()

    # read config
    with open(sys.argv[1], 'r') as infile:
        try:
            config = json.load(infile)
        except json.decoder.JSONDecodeError as err:
            print(f"Invalid JSON: {err}")  # in case json is invalid

    print(config['version'])
    print("Role: " + config['role'])
    print("VTC Platform: " + config['vtc_platform'])

    if config['role'] == 'controller':
        print("Duration: " + str(config['duration']) + " minutes")
        run_controller()

    elif config['role'] == 'client':
        run_client(config)
