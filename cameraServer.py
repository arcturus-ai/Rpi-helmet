#!/usr/bin/env python3
import subprocess
import time
import socket
from urllib.request import urlopen
import os
import RPi.GPIO as GPIO
import signal
import select
from datetime import datetime
import requests

GPIO.cleanup()
GPIO.setmode(GPIO.BCM)
GPIO.setup(18, GPIO.OUT)
GPIO.setup(23, GPIO.OUT)

with open('/etc/hostname', 'r') as file:
    hostname = file.read().strip()
print(f"Hostname: {hostname}")

# RTMP streaming configuration
RTMP_URL = f"rtmp://rtmp.aionasset.in/live/test{hostname}"
FRAMERATE = "20"
RESOLUTION = "640x480"
VIDEO_BITRATE = "600k"
status=0

def start_camera_feed():
    url = "https://www.arcturusbusiness.com/helmetFeed/login/camStart.php"
    files = {"cameraName": (None, hostname)}
    global status
    try:
        response = requests.post(url, files=files)
        response.raise_for_status()  # Raises an error for HTTP error codes
        status=1
        print("on camera api:",response.text)
        return response.text  # Return the API response
    except requests.RequestException as e:
        return f"Error: {e}"
    
def log_message(message):
    """Print message with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

def check_internet():
    """Check if there's an active internet connection"""
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except OSError:
        return False

def wait_for_internet():
    """Wait until internet connection is available"""
    global status
    while not check_internet():
        log_message("Waiting for internet connection...")
        status=0
        time.sleep(1)
    log_message("Internet connection established!")
    if not status:
        start_camera_feed()

def kill_camera_processes():
    """Kill any existing camera processes"""
    try:
        subprocess.run(['pkill', '-f', 'libcamera'], timeout=5)
        subprocess.run(['pkill', '-f', 'ffmpeg'], timeout=5)
        time.sleep(2)
    except subprocess.TimeoutExpired:
        log_message("Warning: Timeout while killing processes")
    except Exception as e:
        log_message(f"Error killing processes: {e}")

def monitor_process_output(process, process_name):
    """Monitor process stderr for errors using select"""
    if process.stderr:
        readable, _, _ = select.select([process.stderr], [], [], 0)
        if readable:
            error_output = process.stderr.readline().decode().strip()
            if error_output:
                # Ignore INFO messages from libcamera
                if process_name == "libcamera" and "INFO" in error_output:
                    log_message(f"libcamera info: {error_output}")
                    return False
                # Check for specific error conditions
                if any(error_term in error_output.lower() for error_term in 
                      ['error', 'failed', 'cannot', 'unable', 'timeout', 'terminated']):
                    log_message(f"{process_name} error: {error_output}")
                    return True
                # Log other messages as debug info
                log_message(f"{process_name} output: {error_output}")
    return False

def start_streaming():
    """Start the RTMP stream using libcamera and ffmpeg"""
    try:
        kill_camera_processes()
        
        libcamera_command = [
            'libcamera-vid',
            '-t', '0',
            '--inline',
            '--nopreview',
            '--rotation','180',
            '--width', RESOLUTION.split('x')[0],
            '--height', RESOLUTION.split('x')[1],
            '--framerate', FRAMERATE,
            '--codec', 'h264',
            '--bitrate', VIDEO_BITRATE,
            '-o', '-'
        ]
        
        ffmpeg_command = [
            'ffmpeg',
            '-i', '-',
            '-c:v', 'copy',
            '-f', 'flv',
            '-loglevel', 'warning',  # Only show warnings and errors
            RTMP_URL
        ]
        
        log_message("Starting libcamera process...")
        libcamera_process = subprocess.Popen(
            libcamera_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        time.sleep(1)
        if libcamera_process.poll() is not None:
            error_output = libcamera_process.stderr.read().decode()
            log_message(f"libcamera failed to start: {error_output}")
            return None
            
        log_message("Starting ffmpeg process...")
        ffmpeg_process = subprocess.Popen(
            ffmpeg_command,
            stdin=libcamera_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        libcamera_process.stdout.close()
        
        log_message("Streaming started successfully")
        return (libcamera_process, ffmpeg_process)
    except Exception as e:
        log_message(f"Error starting stream: {str(e)}")
        return None

def main():
    count = 0
    stream_start_time = None
    log_message("Starting RTMP streaming service")
    
    try:
        kill_camera_processes()
        
        while True:
            wait_for_internet()
            
            max_retries = 3
            for retry in range(max_retries):
                processes = start_streaming()
                if processes:
                    break
                log_message(f"Retry {retry + 1}/{max_retries}")
                time.sleep(3)
            
            if processes:
                libcamera_process, ffmpeg_process = processes
                GPIO.output(18, GPIO.HIGH)
                stream_start_time = time.time()
                log_message("Stream started")
                
                while True:
                    uptime = int(time.time() - stream_start_time)
                    log_message(f"Stream uptime: {uptime} seconds (loop count: {count})")
                    count += 1
                    
                    # Check process status
                    libcamera_status = libcamera_process.poll()
                    ffmpeg_status = ffmpeg_process.poll()
                    
                    if libcamera_status is not None:
                        log_message(f"libcamera process exited with status: {libcamera_status}")
                        error_output = libcamera_process.stderr.read().decode()
                        log_message(f"libcamera final output: {error_output}")
                        GPIO.output(18, GPIO.LOW)
                        kill_camera_processes()
                        break
                        
                    if ffmpeg_status is not None:
                        log_message(f"ffmpeg process exited with status: {ffmpeg_status}")
                        error_output = ffmpeg_process.stderr.read().decode()
                        log_message(f"ffmpeg final output: {error_output}")
                        GPIO.output(18, GPIO.LOW)
                        kill_camera_processes()
                        break
                    
                    # Monitor process output for errors
                    if monitor_process_output(libcamera_process, "libcamera") or \
                       monitor_process_output(ffmpeg_process, "ffmpeg"):
                        log_message("Error detected in process output")
                        GPIO.output(18, GPIO.LOW)
                        kill_camera_processes()
                        break
                    
                    if not check_internet():
                        log_message("Internet connection lost")
                        GPIO.output(18, GPIO.LOW)
                        kill_camera_processes()
                        break
                    
                    time.sleep(3)
            
            log_message("Restarting streaming service in 3 seconds...")
            time.sleep(3)
    except Exception as e:
        log_message(f"Exited with: {e}")
    finally:
        kill_camera_processes()
        GPIO.cleanup()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_message("\nReceived keyboard interrupt, shutting down...")
    finally:
        GPIO.cleanup()
        log_message("Process exited")