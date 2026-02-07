import subprocess
import sys
import time
import select
from app.config import settings

def start_temp_server() -> subprocess.Popen:
    try:
        # We don't have to wait here until config is fully processed and ready,
        # we do spawn the temporary server eitherway just making sure that during the boot
        # nginx does not fail to resolve it, we don't need extra logic
        # such as 
        #    should_start_web_app = (
        #        settings.is_web_api_enabled()
        #        or telegram_webhook_enabled
        #        or payment_webhooks_enabled
        #        or settings.get_miniapp_static_path().exists()
        #    )
        # here...
        temp_server = subprocess.Popen(
            [sys.executable, 'temp_server.py', str(settings.WEB_API_PORT), str(settings.WEB_API_HOST)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        );

        if temp_server.stdout:
            start_time = time.time();
            while time.time() - start_time < 5:
                if temp_server.poll() is not None:
                    print(f'Temporary server exited unexpectedly with code {temp_server.returncode}', flush=True);
                    break;
                ready, _, _ = select.select([temp_server.stdout], [], [], 0.1);
                if ready:
                    line = temp_server.stdout.readline();
                    if 'listening' in line.lower():
                        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Temporay server: {line.strip()}');
                        break;

        return temp_server;
    except Exception as e:
        print(f'Failed to start temporary server: {e}', flush=True);
        return None;


def stop_temp_server(temp_server: subprocess.Popen, timeout: float = 2, stage=None, logger=None) -> None:
    if temp_server is None or temp_server.poll() is not None:
        return;

    try:
        stage.log('Stopping the temporary server...') if stage else (logger.info('Trying to stop the temporary server...') if logger else None);
        temp_server.terminate();
        temp_server.wait(timeout=timeout);
        stage.log('Temporary server has been stopped.') if stage else None;
    except subprocess.TimeoutExpired:
        temp_server.kill();
        temp_server.wait();
        stage.warning('Temporary server was forcibly terminated') if stage else (logger.warning('Temporary server was forcibly terminated') if logger else None);
    except Exception as e:
        stage.warning(f'Failed to stop temporary server: {e}') if stage else (logger.error(f'Failed to stop temporary server: {e}') if logger else None);