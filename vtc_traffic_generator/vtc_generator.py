import ffmpeg
import pulsectl
import time
import json

# Load config(s)
with open('config.json', 'r') as infile:
    config = json.load(infile)

# Initialize client, call from controller
# Set pulse audio devices and device volume
# Check for v4l2 kernel mod
def initialize_vtc_client():
    print ("Initializing VTC client")

def play_audio():
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

def play_video():
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