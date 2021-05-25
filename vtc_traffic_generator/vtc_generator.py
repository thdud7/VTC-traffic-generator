import ffmpeg
import subprocess
import pulsectl
import os
import random
import signal
import sys
import time
import json
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCServer
from pathlib import PurePath

class VtcClient:
    print("Creating client object")
    def __init__(self, ip='0.0.0.0', port=100):
        self.ip = ip
        self.port = int(port)
        self.video_pid = 0


# XMLRPC
# Initialize client
# Set pulse audio devices and device volume
# Check for v4l2 kernel mod
def initialize_vtc_client():
    print ("Initializing VTC client")

    # Set up audio devices
    pulse = pulsectl.Pulse()
    sinks = pulse.sink_list()
    sources = pulse.source_list()

    if "name='virtual_speaker'" not in str(sinks) or "name='virtual_mic'" not in str(sources):
        print("Creating PulseAudio virtual devices")
        subprocess.run('pactl load-module module-null-sink sink_name="virtual_speaker" sink_properties=device.description="virtual_speaker"', capture_output=True, shell=True)
        subprocess.run('pactl load-module module-remap-source master="virtual_speaker.monitor" source_name="virtual_mic" source_properties=device.description="virtual_mic"', capture_output=True, shell=True)

    # Set volume levels and unmute devices
    for sink in sinks:
        pulse.volume_set_all_chans(sink, .7)
        pulse.mute(sink, False)

    for source in sources:
        pulse.volume_set_all_chans(source, .7)
        pulse.mute(source, False)

    # Check for v4l2 virtual webcam kernel module
    if 'v4l2loopback' not in str(subprocess.run(['lsmod'], capture_output=True)):
        print("Error: v4l2loopback kernel module not loaded. Try: sudo modprobe v4l2loopback video_nr=5 exclusive_caps=1")

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
        filename = "line_" + str(index) + ".flac"
        audiofile_fullpath = str(PurePath(convo_path, filename))

        # Play audio
        play_audio(audiofile_fullpath)
        index += 1

    return True

# No XMLRPC needed, simply a local function on the remote VTC client
def play_audio(audio_file_path):
    try:
        process = (
            ffmpeg
                .input(audio_file_path)
                .output('virtual_speaker', format='pulse', device='virtual_speaker')
        )
        process = process.run(capture_stdout=True, capture_stderr=True)

    except ffmpeg.Error as e:
        print('stdout:', e.stdout.decode('utf8'))
        print('stderr:', e.stderr.decode('utf8'))
        raise e


# XMLRPC
def play_video():
    # Setup streaming from file to v4l2 device
    if config['videoconference'] == True:
        try:
            video_filepath = str(PurePath(config['video_path'], config['video_name']))
            print(video_filepath)
            time.sleep(5)

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
def stop_video(video_pid):
    print("Stopping video")
    os.kill(video_pid, signal.SIGTERM)

# XMLRPC
def get_name():
    return config['bot_name']

def run_controller():

    num_clients = len(config['vtc_clients'])
    VTC_clients = []

    # Instantiate clients
    for x in range(num_clients):

        # Create VTC client
        VTC_clients.append(VtcClient(config['vtc_clients'][x][0], config['vtc_clients'][x][1]))

        # Initialize client devices
        uri = 'http://' + VTC_clients[x].ip + ':' + str(VTC_clients[x].port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.initialize_vtc_client()

        # Start client video stream to virtual camera device
        if config['videoconference'] == True:
            with xmlrpc.client.ServerProxy(uri) as proxy:
                VTC_clients[x].video_pid = proxy.play_video()

        '''
        # Testing Video stopping capability
        time.sleep(15)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.stop_video(VTC_clients[x].video_pid)
        '''

    # Begin client dialog
    # Randomly select VTC_client as long as it wasn't the last one picked.
    chosen_client = None
    candidate_client = random.choice(VTC_clients)

    # Infinite loop of conversation dialog.
    while True:
        while candidate_client is chosen_client:
            candidate_client = random.choice(VTC_clients)

        chosen_client = candidate_client
        uri = 'http://' + chosen_client.ip + ':' + str(chosen_client.port)

        # Print bot name on controller STDOUT for debugging / manual bot admittance
        with xmlrpc.client.ServerProxy(uri) as proxy:
            print(proxy.get_name() + " speaking now.")

        # Command selected VTC client to take a dialog cycle
        with xmlrpc.client.ServerProxy(uri) as proxy:
            dialog_complete = False
            dialog_complete = proxy.dialog_cycle()


def run_client(config):
    # Register functions and respond to calls indefinitely
    server = SimpleXMLRPCServer(("0.0.0.0", config['c2_port']), allow_none=True)
    print("Listening on port: " + str(config['c2_port']))

    server.register_function(initialize_vtc_client, "initialize_vtc_client")
    server.register_function(play_video, "play_video")
    server.register_function(stop_video, "stop_video")
    server.register_function(dialog_cycle, "dialog_cycle")
    server.register_function(get_name, "get_name")

    server.serve_forever()


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
    print ("Role: " + config['role'])

    if config['role'] == 'controller':
        run_controller()

    elif config['role'] == 'client':
        run_client(config)
