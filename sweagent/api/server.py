from __future__ import annotations

import io
import json
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from uuid import uuid4
from queue import Queue, Empty

import flask
import yaml
from flask import Flask, make_response, render_template, request, session, Response, stream_with_context
from flask_cors import CORS

from sweagent import CONFIG_DIR, PACKAGE_DIR
from sweagent.agent.problem_statement import problem_statement_from_simplified_input
from sweagent.api.utils import AttrDict, ThreadWithExc
from sweagent.environment.repo import repo_from_simplified_input
from sweagent.run.hooks.abstract import RunHook
from sweagent.agent.hooks.abstract import AbstractAgentHook
from sweagent.environment.hooks.abstract import EnvHook

# baaaaaaad
sys.path.append(str(PACKAGE_DIR.parent))
from sweagent.run.run_single import RunSingle, RunSingleConfig

app = Flask(__name__, template_folder=Path(__file__).parent)
CORS(app)
app.secret_key = "super secret key"
app.config["SESSION_TYPE"] = "memcache"

# 각 세션마다 실행 쓰레드를 저장합니다.
THREADS: dict[str, MainThread] = {}
# SSE 이벤트를 저장할 큐 (세션별)
SSE_QUEUES: dict[str, Queue] = {}

def get_sse_queue(session_id: str) -> Queue:
    if session_id not in SSE_QUEUES:
        SSE_QUEUES[session_id] = Queue()
    return SSE_QUEUES[session_id]

class StreamToSSE(io.StringIO):
    """
    stdout/stderr를 SSE로 전송하기 위한 스트림.
    쓰여지는 메시지가 있을 때마다 SSEWebUpdate.up_log를 호출합니다.
    """
    def __init__(self, wu: SSEWebUpdate):
        super().__init__()
        self._wu = wu

    def write(self, message):
        if message.strip():
            self._wu.up_log(message)

    def flush(self):
        pass

class SSEWebUpdate:
    """HTTP SSE를 통해 클라이언트에 이벤트를 전송하는 클래스."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.queue = get_sse_queue(session_id)
        self.log_stream = StreamToSSE(self)

    def _emit(self, event: str, data: dict):
        """이벤트를 큐에 추가합니다."""
        event_data = {
            'event': event,
            'data': data,
        }
        self.queue.put(event_data)

    def up_log(self, message: str):
        self._emit("log", {"message": message})

    def up_banner(self, message: str):
        self._emit("banner", {"message": message})

    def up_agent(self, message: str, *, format: str = "markdown", thought_idx: int | None = None, type_: str = "info"):
        self._emit("agent", {
            "message": message,
            "format": format,
            "thought_idx": thought_idx,
            "type": type_,
        })

    def up_env(self, message: str, *, type_: str, format: str = "markdown", thought_idx: int | None = None):
        self._emit("env", {
            "message": message,
            "format": format,
            "thought_idx": thought_idx,
            "type": type_,
        })

    def finish_run(self):
        self._emit("finish", {})

class MainUpdateHook(RunHook):
    def __init__(self, wu: SSEWebUpdate):
        """This hooks into the Main class to update the web interface"""
        self._wu = wu

    def on_start(self):
        self._wu.up_env(message="Environment container initialized", format="text", type_="info")

    def on_end(self):
        self._wu.up_agent(message="The run has ended", format="text")
        self._wu.finish_run()

    def on_instance_completed(self, *, info, trajectory):
        print(info.get("submission"))
        if info.get("submission") and info["exit_status"] == "submitted":
            msg = (
                "The submission was successful. You can find the patch (diff) in the right panel. "
                "To apply it to your code, run `git apply /path/to/patch/file.patch`. "
            )
            self._wu.up_agent(msg, type_="success")


class AgentUpdateHook(AbstractAgentHook):
    def __init__(self, wu: SSEWebUpdate):
        """This hooks into the Agent class to update the web interface"""
        self._wu = wu
        self._sub_action = None
        self._thought_idx = 0

    def on_actions_generated(self, *, thought: str, action: str, output: str):
        self._thought_idx += 1
        for prefix in ["DISCUSSION\n", "THOUGHT\n", "DISCUSSION", "THOUGHT"]:
            thought = thought.replace(prefix, "")
        self._wu.up_agent(
            message=thought,
            format="markdown",
            thought_idx=self._thought_idx,
            type_="thought",
        )

    def on_sub_action_started(self, *, sub_action: dict):
        # msg = f"```bash\n{sub_action['action']}\n```"
        msg = "$ " + sub_action["action"].strip()
        self._sub_action = sub_action["action"].strip()
        self._wu.up_env(message=msg, thought_idx=self._thought_idx, type_="command")

    def on_sub_action_executed(self, *, obs: str, done: bool):
        type_ = "output"
        if self._sub_action == "submit":
            type_ = "diff"
        if obs is None:
            # This can happen for empty patch submissions
            obs = ""
        msg = obs.strip()
        self._wu.up_env(message=msg, thought_idx=self._thought_idx, type_=type_)


class EnvUpdateHook(EnvHook):
    def __init__(self, wu: SSEWebUpdate):
        """This hooks into the environment class to update the web interface"""
        self._wu = wu

    def on_close(self):
        self._wu.up_env(message="Environment closed", format="text", type_="info")

def ensure_session_id_set():
    """세션 ID가 설정되어 있지 않으면 생성하여 반환합니다."""
    session_id = session.get("session_id", None)
    if not session_id:
        session_id = uuid4().hex
        session["session_id"] = session_id
    return session_id

class MainThread(ThreadWithExc):
    def __init__(self, settings: RunSingleConfig, wu: SSEWebUpdate):
        super().__init__()
        self._wu = wu
        self._settings = settings

    def run(self) -> None:
        # stdout과 stderr를 SSE를 통해 클라이언트에 전송
        with redirect_stdout(self._wu.log_stream):
            with redirect_stderr(self._wu.log_stream):
                try:
                    main = RunSingle.from_config(self._settings)
                    main.add_hook(MainUpdateHook(self._wu))
                    main.agent.add_hook(AgentUpdateHook(self._wu))
                    main.env.add_hook(EnvUpdateHook(self._wu))
                    main.run()
                except Exception as e:
                    short_msg = str(e)
                    max_len = 350
                    if len(short_msg) > max_len:
                        short_msg = f"{short_msg[:max_len]}... (see log for details)"
                    traceback_str = traceback.format_exc()
                    self._wu.up_log(traceback_str)
                    self._wu.up_agent(f"Error: {short_msg}")
                    self._wu.up_banner("Critical error: " + short_msg)
                    self._wu.finish_run()
                    raise

    def stop(self):
        while self.is_alive():
            self.raise_exc(SystemExit)
            time.sleep(0.1)
        self._wu.finish_run()
        self._wu.up_agent("Run stopped by user")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/stream")
def stream():
    session_id = ensure_session_id_set()
    queue = get_sse_queue(session_id)

    def event_stream():
        while True:
            try:
                # 큐에서 이벤트를 가져오고 SSE 형식으로 변환하여 전송
                event = queue.get(timeout=1)
                yield f"event: {event['event']}\n"
                yield f"data: {json.dumps(event['data'])}\n\n"
            except Empty:
                # keep-alive 주석 전송
                yield ": keep-alive\n\n"

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

@app.route("/run", methods=["GET", "OPTIONS"])
def run():
    session_id = ensure_session_id_set()
    if request.method == "OPTIONS":
        return _build_cors_preflight_response()

    # 한 세션 당 한 번의 실행만 허용합니다.
    global THREADS
    for thread in THREADS.values():
        if thread.is_alive():
            thread.stop()

    # SSEWebUpdate를 session_id로 생성합니다.
    wu = SSEWebUpdate(session_id)
    wu.up_agent("Starting the run")

    # runConfig 파라미터 파싱 (실제 구성은 필요에 따라 수정)
    run_config: Any = AttrDict.from_nested_dicts(json.loads(request.args["runConfig"]))
    print(run_config)
    print(run_config.environment)
    print(run_config.environment.base_commit)
    model_name: str = run_config.agent.model.model_name
    test_run: bool = run_config.extra.test_run
    if test_run:
        model_name = "instant_empty_submit"
    default_config = yaml.safe_load(Path(CONFIG_DIR / "default_from_url.yaml").read_text())
    config = {
        **default_config,
        "agent": {
            "model": {
                "model_name": model_name,
            },
        },
        "environment": {
            "image_name": run_config.environment.image_name,
            "script": run_config.environment.script,
        },
    }
    config["problem_statement"] = problem_statement_from_simplified_input(
        input=run_config.problem_statement.input,
        type=run_config.problem_statement.type,
    )
    config["environment"]["repo"] = repo_from_simplified_input(
        input=run_config.environment.repo_path,
        base_commit=run_config.environment.base_commit,
        type="auto",
    )
    config = RunSingleConfig.model_validate(**config)
    thread = MainThread(config, wu)
    THREADS[session_id] = thread
    thread.start()
    return "Commands are being executed", 202

@app.route("/stop")
def stop():
    session_id = ensure_session_id_set()
    global THREADS
    print(f"Stopping session {session_id}")
    print(THREADS)
    thread = THREADS.get(session_id)
    if thread and thread.is_alive():
        print(f"Thread {thread} is alive")
        thread.stop()
    else:
        print(f"Thread {thread} is not alive")
    return "Stopping computation", 202

def _build_cors_preflight_response():
    response = make_response()
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "*")
    response.headers.add("Access-Control-Allow-Methods", "*")
    return response

def run_from_cli(args: list[str] | None = None):
    app.run(port=8000, debug=True)

if __name__ == "__main__":
    run_from_cli()