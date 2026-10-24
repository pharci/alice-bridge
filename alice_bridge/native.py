"""Native Station CLI transport; no model or agent dependencies."""
import re
import subprocess
import threading
from .speech import speech_text

class NativeCLI:
    def __init__(self, adb: str, serial: str):
        self.adb, self.serial = adb, serial
        self.process = None
        self.lock = threading.Lock()
        self.ready = threading.Event()

    def start(self):
        if self.process and self.process.poll() is None:
            return
        self.ready.clear()
        command = ('LD_LIBRARY_PATH=/system/vendor/quasar:/system/vendor/quasar/libs '
                   '/system/vendor/quasar/maind --cli')
        self.process = subprocess.Popen([self.adb, '-s', self.serial, 'shell', command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        process = self.process
        def drain():
            try:
                for line in iter(process.stdout.readline, b''):
                    # Discard device logs, which include device identifiers.
                    if b'Connected to DirectiveSequencer' in line:
                        self.ready.set()
            finally:
                if self.process is process:
                    self.ready.clear()
        threading.Thread(target=drain, name='native-cli', daemon=True).start()
        if not self.ready.wait(8):
            self.close()
            raise RuntimeError('Native Alice CLI did not become ready')

    def send(self, value: str):
        with self.lock:
            if not self.process or self.process.poll() is not None:
                self.start()
            self.process.stdin.write(value.encode('utf-8'))
            self.process.stdin.flush()

    def cancel(self):
        self.send('c\n')

    def speak(self, value: str):
        safe = speech_text(value)
        if not safe:
            raise ValueError('Empty speech')
        # The CLI reads a command line followed by a separate query line.
        # Collapsing whitespace prevents model text becoming CLI commands.
        self.send('q\nповтори за мной ' + safe + '\n')

    def audio_status(self) -> dict:
        # These are native persisted controls, not ALSA's idle mixer values.
        # The final file is a one-byte 0/1 flag; the preceding digits are the
        # vendor volume index, whose scale must not be assumed to be percent.
        result = subprocess.run([self.adb, '-s', self.serial, 'shell',
            'cat /data/quasar/data/last_vendor_volume /data/quasar/data/is_speaker_muted'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        raw = result.stdout.decode('ascii').strip()
        if result.returncode or not re.fullmatch(r'\d+[01]', raw):
            raise RuntimeError('Native speaker controls are unavailable')
        return {'volume_raw': int(raw[:-1]), 'muted': raw[-1] == '1'}

    def close(self):
        process, self.process = self.process, None
        if process and process.poll() is None:
            try:
                process.stdin.write(b'x\n')
                process.stdin.flush()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()

