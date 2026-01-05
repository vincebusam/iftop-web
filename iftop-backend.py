#!/usr/bin/env python3
"""
iftop WebSocket Backend

Installation requirements:
    pip install websockets

Usage:
    python iftop_backend.py

The server will listen on ws://localhost:8766
"""

import asyncio
import ipaddress
import json
import subprocess
import websockets


def load_hosts():
    hosts = {}
    try:
        ethers = dict(map(lambda x: x.strip().split(), open("/usr/local/etc/ethers").readlines()))
    except FileNotFoundError as e:
        ethers = {}
    try:
        res = subprocess.run(["dhcp-lease-list"], capture_output=True, text=True, check=True)
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                ipaddress.ip_address(parts[1])
            except ValueError:
                continue
            hosts[parts[1]] = parts[2]
            if parts[0] in ethers:
                hosts[parts[1]] = ethers[parts[0]]
    except FileNotFoundError as e:
        pass
    return hosts


def classify(conn, hosts):
    for h in ["txhost", "rxhost"]:
        if conn[h] in hosts:
            conn[h] += f"[{hosts[conn[h]]}]"

    for port, desc in [
        (22, "SSH"),
        (80, "HTTP"),
        (143, "IMAP"),
        (443, "HTTPS"),
        (16393, "FaceTime"),
        (25565, "MineCraft"),
    ]:
        if int(conn.get("txport") or 0) == port or int(conn.get("rxport") or 0) == port:
            conn["description"] = desc
    return


def parse_rate(rate: str) -> float:
    if rate.endswith("Gb") or rate.endswith("GB"):
        return float(rate[:-2]) * 1000000000
    if rate.endswith("Mb") or rate.endswith("MB"):
        return float(rate[:-2]) * 1000000
    if rate.endswith("Kb") or rate.endswith("KB"):
        return float(rate[:-2]) * 1000
    if rate.endswith("b") or rate.endswith("B"):
        return float(rate[:-1])
    try:
        return float(rate)
    except ValueError:
        return 0


async def run_iftop(interface: str, websocket):
    """
    Run iftop test for a specific interface.
    
    Args:
        interface: network interface name
        websocket: WebSocket connection to send updates
    """
    try:
        # Build iftop command
        cmd = [
            'iftop',
            '-i', interface,
            '-tPN',
        ]
        
        # Start iftop process
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        await websocket.send(json.dumps({
            "connections": [{
                "description": "Waiting",
                "txhost": "--",
                "txport": "-",
                "rxhost": "--",
                "rxport": "-",
            }],
            "total": {
                "tx2s": 0,
                "tx10s": 0,
                "tx40s": 0,
                "rx2s": 0,
                "rx10s": 0,
                "rx40s": 0,
                "2s": 0,
                "10s": 0,
                "40s": 0,
            },
            "peak": {
                "2s": 0,
                "10s": 0,
                "40s": 0,
            }
        }))

        hosts = load_hosts()

        data = {}
        cnt = 0
        # Read output line by line
        while True:
            line = await process.stdout.readline()
            line = line.decode('utf-8').strip()
            if not line:
                continue

            parts = line.split()
           
            try:
                if line.startswith("===="):
                    for conn in data.get("connections", []):
                        classify(conn, hosts)
                    data["connections"].insert(0, {
                        **data["total"],
                        "description": "Total",
                        "txhost": "0.0.0.0",
                        "txport": "0",
                        "rxhost": "0.0.0.0",
                        "rxport": "0",
                    })
                    await websocket.send(json.dumps(data))
                    data.clear()
                    cnt += 1
                    if cnt >= 300:  # End after 10 minutes
                        process.terminate()
                        break
                    continue

                if "=>" in parts or "<=" in parts:
                    if parts[0].isdigit():
                        data.setdefault("connections", []).append({
                            "description": "Unknown",
                        })
                        parts.pop(0)
                    d = "tx" if "=>" in parts else "rx"
                    data["connections"][-1].update({
                        f"{d}host": parts[0].split(":")[0],
                        f"{d}port": parts[0].split(":")[1],
                        f"{d}2s": parse_rate(parts[2]),
                        f"{d}10s": parse_rate(parts[3]),
                        f"{d}40s": parse_rate(parts[4]),
                        f"{d}cum": parse_rate(parts[5]),
                    })
                    continue

                if "Total send rate" in line:
                    data["total"] = {
                        "tx2s": parse_rate(parts[3]),
                        "tx10s": parse_rate(parts[4]),
                        "tx40s": parse_rate(parts[5]),
                    }
                    continue

                if "Total receive rate" in line:
                    data["total"].update({
                        "rx2s": parse_rate(parts[3]),
                        "rx10s": parse_rate(parts[4]),
                        "rx40s": parse_rate(parts[5]),
                    })
                    continue

                if "Total send and receive rate" in line:
                    data["total"].update({
                        "2s": parse_rate(parts[5]),
                        "10s": parse_rate(parts[6]),
                        "40s": parse_rate(parts[7]),
                    })
                    continue

                if "Peak rate" in line:
                    data["peak"] = {
                        "2s": parse_rate(parts[3]),
                        "10s": parse_rate(parts[4]),
                        "40s": parse_rate(parts[5]),
                    }
                    continue

                if "Cumulative" in line:
                    data["cum"] = {
                        "2s": parse_rate(parts[2]),
                        "10s": parse_rate(parts[3]),
                        "40s": parse_rate(parts[4]),
                    }
                    continue

            except websockets.exceptions.ConnectionClosed:
                process.terminate()
                break
            except Exception as e:
                print(f"Error parsing iftop output: {e}")
                print(line)
                print(parts)
                continue
        
        # Wait for process to complete
        await process.wait()
        
        # Check for errors
        if process.returncode != 0 and False:  # Debugging
            stderr = await process.stderr.read()
            error_msg = stderr.decode('utf-8')
            print(f"iftop error: {error_msg}")
            return 0
        
        return
        
    except websockets.exceptions.ConnectionClosed:
        pass

    except Exception as e:
        print(f"Error running iftop: {e}")
        await websocket.send(json.dumps({
            'type': 'error',
            'interface': interface,
            'message': str(e)
        }))
        return 0


async def handle_client(websocket, path):
    """Handle WebSocket client connection."""
    try:
        async for message in websocket:
            data = json.loads(message)
            interface = data.get('interface')
            
            await run_iftop(interface, websocket)

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"Error handling client: {e}")
        try:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': str(e)
            }))
        except:
            pass


async def main():
    """Start the WebSocket server."""
    print("Starting iftop WebSocket server on ws://localhost:8766")
    print("Make sure iftop is installed and available in your PATH")
    
    async with websockets.serve(handle_client, "0.0.0.0", 8766):
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped")
