import ffmpeg
import subprocess
import pulsectl
import sys
import time
import json

class VTC_client:
    print("Creating client object")
    def __init__(self, ip='0.0.0.0', port=100):
        self.ip = ip
        self.port = int(port)

    # Initialize client, call from controller
    # Set pulse audio devices and device volume
    # Check for v4l2 kernel mod
    def initialize_vtc_client(self):
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


        # Check for v4l2 virtual webcam kernel module


    # XMLRPC needed
    def play_audio(self):
        try:
            process = (
                ffmpeg
                    .input('/home/dave/Desktop/line_9.flac')
                    .output('virtual_speaker', format='pulse', device='virtual_speaker')
            )
            process = process.run(capture_stdout=True, capture_stderr=True)

        except ffmpeg.Error as e:
            print('stdout:', e.stdout.decode('utf8'))
            print('stderr:', e.stderr.decode('utf8'))
            raise e

    # XMLRPC needed
    def play_video(self):
        # Setup streaming from file to v4l2 device
        process = (
            ffmpeg
                .input('man_1.mp4', re=None, stream_loop=-1)
                .filter('format', 'yuv420p')
                .drawtext(text='BOT1', x='(w-text_w)/2', y='h-th-100', fontcolor='red', fontsize=200)
                .output('/dev/video5', format='v4l2')
        )

        # Launch video recording
        process = process.run_async(pipe_stdin=True)

        time.sleep(15)

        # Stop video recording
        process.communicate(b'q')  # Equivalent to send a Q

        # Terminate process after waiting 3s to ensure process end
        time.sleep(3)
        process.terminate()

    # XMLRPC
    def stop_video(self):
        print("Stopping video")

def run_controller():

    num_clients = len(config['vtc_clients'])
    VTC_clients = []

    # Instantiate clients
    for x in range(num_clients):
        VTC_clients.append(VTC_client(config['vtc_clients'][x][0], config['vtc_clients'][x][1]))

    # Initialize clients
    # Start client video
    # Begin client dialog

def run_client():
    print("Running client")
    # Mostly be reactionary

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Error: must specify configuration JSON file.")
        exit()

    # read config
    with open(sys.argv[1], 'r') as infile:
        config = json.load(infile)

    print(config['version'])
    print ("Role: " + config['role'])

    if config['role'] == 'controller':
        run_controller()

    elif config['role'] == 'client':
        run_client()
