import os
import subprocess
import logging

# PYTHON = os.path.join(os.getcwd(), 'venv', 'bin', 'python')  # venv on linux
PYTHON = os.path.join(os.getcwd(), 'venv', 'Scripts', 'python.exe')  # venv on Windows

START_COMMANDS = [
    [PYTHON, '-m', 'pip', 'install', '-r', 'requirements.txt'],
    [PYTHON, 'main.py', '-z', '7']
]

if __name__ == '__main__':
    logging.basicConfig(format='[%(levelname)s] [%(asctime)s] %(msg)s', level=logging.INFO)
    logging.info('starting via launcher')
    for command in START_COMMANDS:
        subprocess.run(command)
