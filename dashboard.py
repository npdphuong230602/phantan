import os
import sys
import subprocess
import requests
from flask import Flask, render_template, request, Response, jsonify, send_from_directory

# Resolve the project directory so all subprocess paths are absolute
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/run_benchmark')
def run_benchmark():
    """Starts the benchmark and streams the output via Server-Sent Events (SSE)."""
    threads = request.args.get('threads', '30')
    runs = request.args.get('runs', '3')
    
    def generate():
        # Build a CLEAN environment for the subprocess.
        # CRITICAL: Remove all WERKZEUG_* variables. When Flask runs with debug=True,
        # it sets WERKZEUG_SERVER_FD (the server socket file descriptor) and
        # WERKZEUG_RUN_MAIN in the environment. If these leak into child processes
        # that are also Flask apps (like site_server.py), those children will try
        # to reuse the dashboard's socket instead of binding their own ports,
        # causing them to silently fail to start.
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        # Remove all Werkzeug internal variables
        for key in list(env.keys()):
            if key.startswith('WERKZEUG_'):
                del env[key]
        
        # Start benchmark.py with unbuffered output (-u)
        benchmark_path = os.path.join(BASE_DIR, "benchmark.py")
        cmd = [sys.executable, "-u", benchmark_path, "--threads", threads, "--runs", runs]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding='utf-8',
            env=env,
            cwd=BASE_DIR
        )
        
        for line in iter(process.stdout.readline, ''):
            if not line:
                break
            clean_line = line.strip()
            if clean_line:
                yield f"data: {clean_line}\n\n"
            
        process.stdout.close()
        process.wait()
        
        # Run visualizer after benchmark finishes
        yield f"data: INFO:============================================================\n\n"
        yield f"data: INFO: Đang khởi chạy vẽ biểu đồ (Visualizer)...\n\n"
        
        visualizer_path = os.path.join(BASE_DIR, "visualizer.py")
        vis_cmd = [sys.executable, "-u", visualizer_path]
        vis_process = subprocess.Popen(
            vis_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding='utf-8',
            env=env,
            cwd=BASE_DIR
        )
        for line in iter(vis_process.stdout.readline, ''):
            if not line:
                break
            clean_line = line.strip()
            if clean_line:
                yield f"data: {clean_line}\n\n"
            
        vis_process.stdout.close()
        vis_process.wait()

        # Signal completion to frontend
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/kill/<int:site>', methods=['POST'])
def kill_site(site):
    port = 5000 + site
    try:
        r = requests.post(f'http://localhost:{port}/admin/kill', timeout=2)
        return jsonify({"status": "success", "message": f"Site {site} KILLED"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/revive/<int:site>', methods=['POST'])
def revive_site(site):
    port = 5000 + site
    try:
        r = requests.post(f'http://localhost:{port}/admin/revive', timeout=2)
        return jsonify({"status": "success", "message": f"Site {site} REVIVED"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/results/<path:filename>')
def serve_results(filename):
    """Serve the generated charts."""
    return send_from_directory(os.path.join(BASE_DIR, 'results'), filename)

# Global reference to the fault demo process so we can kill it on Stop
_demo_process = None

@app.route('/api/fault_demo')
def fault_demo():
    """Starts the Fault Tolerance Live Demo and streams output via SSE."""
    def generate():
        global _demo_process
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        for key in list(env.keys()):
            if key.startswith('WERKZEUG_'):
                del env[key]

        demo_path = os.path.join(BASE_DIR, "fault_demo_runner.py")
        cmd = [sys.executable, "-u", demo_path]
        _demo_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding='utf-8',
            env=env,
            cwd=BASE_DIR
        )

        for line in iter(_demo_process.stdout.readline, ''):
            if not line:
                break
            clean_line = line.strip()
            if clean_line:
                yield f"data: {clean_line}\n\n"

        _demo_process.stdout.close()
        _demo_process.wait()
        _demo_process = None
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/stop_demo', methods=['POST'])
def stop_demo():
    """Stops the running fault demo."""
    global _demo_process
    if _demo_process and _demo_process.poll() is None:
        _demo_process.terminate()
        try:
            _demo_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _demo_process.kill()
        _demo_process = None
        return jsonify({"status": "success", "message": "Demo stopped"})
    return jsonify({"status": "info", "message": "No demo running"})

if __name__ == '__main__':
    app.run(port=5000, debug=False, threaded=True)

