#!/usr/bin/env python3
from aiohttp import web
import asyncio
import signal
import sys

should_stop = False;

def sig_handler(signum, frame):
    global should_stop;
    should_stop = True;

async def temp_handler(request):
    return web.json_response({ 'status': 'initializing', 'ready': False }, status=404);

async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080;
    host = sys.argv[2] if len(sys.argv) > 2 else '0.0.0.0';

    app = web.Application();
    app.router.add_route('*', '/{path:.*}', temp_handler);

    runner = web.AppRunner(app);
    await runner.setup();
    site = web.TCPSite(runner, host, port);
    await site.start();
    print(f'Temporary server listening on {host}:{port}', flush=True);

    while not should_stop:
        await asyncio.sleep(0.01);

    await site.stop();
    await runner.cleanup();
    print('Temporary server has been stopped...', flush=True);

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, sig_handler);
    signal.signal(signal.SIGINT, sig_handler);

    try:
        asyncio.run(main());
    except KeyboardInterrupt:
        pass;