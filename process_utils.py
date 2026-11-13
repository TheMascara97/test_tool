# -*- coding: utf-8 -*-
"""可取消的等待与子进程执行工具，不依赖 GUI 框架。"""

import logging
import os
import queue
import signal
import subprocess
import threading
import time


def sleep_interruptible(stop_event, seconds):
    """等待指定秒数；stop_event 被设置时立即返回 True。"""
    if stop_event is None:
        time.sleep(max(0.0, seconds))
        return False
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if stop_event.is_set():
            return True
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


class ProcessResult:
    """子进程的统一结果。"""

    def __init__(self, return_code=None, output='', reason='completed', error=''):
        self.return_code = return_code
        self.output = output
        self.reason = reason
        self.error = error

    @property
    def success(self):
        return self.reason == 'completed' and self.return_code == 0

    @property
    def cancelled(self):
        return self.reason == 'cancelled'

    @property
    def timed_out(self):
        return self.reason == 'timeout'

    @property
    def stopped(self):
        return self.reason == 'stopped'


def terminate_process_tree(process, label='子进程'):
    """终止进程及其子进程，并在 Windows 上按 PID 清理整棵进程树。"""
    if process is None:
        return
    pid = getattr(process, 'pid', None)
    try:
        if os.name == 'nt' and pid:
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        elif os.name != 'nt' and pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.terminate()
    except Exception as ex:
        logging.warning("%s PID=%s 进程树终止失败，回退到 kill: %s", label, pid, ex)

    try:
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=1)
        except Exception:
            pass


def run_cancellable_process(command, stop_event=None, timeout=None, cwd=None,
                            env=None, on_output=None, on_poll=None,
                            on_start=None, on_finish=None, label='子进程',
                            **popen_overrides):
    """
    持续读取子进程输出，并支持停止、超时和进程树清理。

    回调约定：
    - on_output(line) 返回 True 时请求停止，结果 reason='stopped'
    - on_poll(elapsed) 每 0.2 秒调用一次，可用于日志、心跳和状态刷新
    - on_start(process) 在进程启动后调用
    - on_finish(process) 在进程退出和输出排空后调用
    """
    stop_event = stop_event if stop_event is not None else threading.Event()

    stdout = popen_overrides.pop('stdout', subprocess.PIPE)
    stderr = popen_overrides.pop('stderr', subprocess.STDOUT)
    stdin = popen_overrides.pop('stdin', subprocess.DEVNULL)
    text = popen_overrides.pop('text', True)
    encoding = popen_overrides.pop('encoding', 'utf-8')
    errors = popen_overrides.pop('errors', 'replace')
    bufsize = popen_overrides.pop('bufsize', 1)

    popen_kwargs = {
        'stdout': stdout,
        'stderr': stderr,
        'stdin': stdin,
        'text': text,
        'encoding': encoding if text else None,
        'errors': errors if text else None,
        'cwd': cwd,
        'env': env,
        'bufsize': bufsize,
    }
    if not text:
        popen_kwargs.pop('encoding')
        popen_kwargs.pop('errors')
    if os.name == 'nt':
        popen_kwargs['creationflags'] = (
            getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        )
    else:
        popen_kwargs['start_new_session'] = True
    popen_kwargs.update(popen_overrides)

    process = None
    finish_called = False

    def call_callback(callback, *args):
        if callback is None:
            return False
        try:
            return bool(callback(*args))
        except Exception as ex:
            logging.warning("%s 回调异常: %s", label, ex, exc_info=True)
            return False

    try:
        if stop_event.is_set():
            return ProcessResult(reason='cancelled')

        try:
            process = subprocess.Popen(command, **popen_kwargs)
        except Exception as ex:
            logging.error("%s 启动失败: %s", label, ex)
            return ProcessResult(reason='error', error=str(ex))

        if call_callback(on_start, process):
            terminate_process_tree(process, label)
            return ProcessResult(reason='error', error='on_start callback failed')

        output_queue = queue.Queue()

        def read_output():
            try:
                if text:
                    for line in iter(process.stdout.readline, ''):
                        output_queue.put(line)
                else:
                    while True:
                        chunk = process.stdout.read(4096)
                        if not chunk:
                            break
                        output_queue.put(chunk)
            except Exception as ex:
                output_queue.put(('\n[读取输出异常] %s\n' % ex) if text else b'')
            finally:
                output_queue.put(None)

        reader = threading.Thread(
            target=read_output,
            name='%s-OutputReader' % label,
            daemon=True,
        )
        reader.start()

        output_parts = []
        stream_finished = False
        reason = 'completed'
        error = ''
        start_time = time.monotonic()
        last_poll = 0.0

        def consume_line(line):
            nonlocal reason
            if line is None:
                return
            output_parts.append(line)
            if call_callback(on_output, line):
                reason = 'stopped'

        try:
            while True:
                if stop_event.is_set():
                    reason = 'cancelled'
                    break

                now = time.monotonic()
                elapsed = now - start_time
                if timeout is not None and elapsed > timeout:
                    reason = 'timeout'
                    break

                if on_poll is not None and now - last_poll >= 0.2:
                    call_callback(on_poll, elapsed)
                    last_poll = now

                # 每轮排空现有输出，避免高输出量时读取滞后。
                while True:
                    try:
                        line = output_queue.get_nowait()
                    except queue.Empty:
                        break
                    if line is None:
                        stream_finished = True
                    else:
                        consume_line(line)
                    if reason == 'stopped':
                        break

                if reason == 'stopped':
                    break
                if process.poll() is not None and stream_finished:
                    break
                time.sleep(0.05)

            if reason != 'completed':
                logging.warning("%s PID=%s 正在终止，原因: %s", label, process.pid, reason)
                terminate_process_tree(process, label)
            else:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    reason = 'timeout'
                    terminate_process_tree(process, label)

            # 子进程退出后读取线程可能仍有尾部输出，必须 join 后继续排空。
            reader.join(timeout=2)
            while True:
                try:
                    line = output_queue.get_nowait()
                except queue.Empty:
                    break
                if line is None:
                    stream_finished = True
                else:
                    consume_line(line)
            if not stream_finished and reader.is_alive():
                try:
                    process.stdout.close()
                except Exception:
                    pass
                reader.join(timeout=1)
                while True:
                    try:
                        line = output_queue.get_nowait()
                    except queue.Empty:
                        break
                    if line is None:
                        stream_finished = True
                    else:
                        consume_line(line)

            return ProcessResult(
                return_code=process.returncode,
                output=''.join(output_parts),
                reason=reason,
                error=error,
            )
        except Exception as ex:
            error = str(ex)
            logging.error("%s 执行异常: %s", label, ex, exc_info=True)
            terminate_process_tree(process, label)
            reader.join(timeout=1)
            while True:
                try:
                    line = output_queue.get_nowait()
                except queue.Empty:
                    break
                if line is not None:
                    output_parts.append(line)
            return ProcessResult(
                return_code=process.returncode,
                output=''.join(output_parts),
                reason='error',
                error=error,
            )
    finally:
        if not finish_called:
            finish_called = True
            if process is not None and process.poll() is None:
                terminate_process_tree(process, label)
            call_callback(on_finish, process)
            try:
                if process is not None and process.stdout is not None:
                    process.stdout.close()
            except Exception:
                pass
