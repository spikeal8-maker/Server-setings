"""Bounded local-only AI workload; no automatic provider retries or global interrupt."""
import codecs
import hashlib
import json
import struct
import time
import urllib.request
import uuid
from release_load_contracts import ContractError, contract, require, local_ai, safe_code, png_dimensions


class SseDecoder:
    def __init__(self, request_id):
        self.request_id = request_id
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.buffer = ''
        self.done = None
        self.done_count = 0
        self.text = ''
        self.comments = 0

    def feed(self, raw):
        self.buffer += self.decoder.decode(raw)
        require(len(self.buffer) <= 1024 * 1024, 'SSE frame exceeds workload bound')
        self.buffer = self.buffer.replace('\r\n', '\n')
        while '\n\n' in self.buffer:
            frame, self.buffer = self.buffer.split('\n\n', 1)
            lines = frame.split('\n')
            self.comments += sum(line.startswith(':') for line in lines)
            event = next((v[6:].strip() for v in lines if v.startswith('event:')), None)
            body = '\n'.join(v[5:].lstrip() for v in lines if v.startswith('data:'))
            if not event or not body:
                continue
            data = json.loads(body)
            require(isinstance(data, dict), 'invalid SSE data shape')
            if event == 'error':
                raise ContractError('SSE error: ' + safe_code(data.get('code')))
            if event == 'delta':
                require(self.done is None and isinstance(data.get('text'), str), 'invalid/late SSE delta')
                self.text += data['text']
                require(len(self.text) <= 65536, 'SSE output exceeds workload bound')
            elif event == 'done':
                self.done_count += 1
                require(self.done_count == 1 and isinstance(data.get('messages'), list), 'duplicate/invalid SSE completion')
                require(data.get('stopped') is False, 'AI completion stopped before answer')
                messages = data['messages']
                require(all(m.get('requestId') == self.request_id for m in messages), 'AI completion contains another request')
                require(any(m.get('role') == 'assistant' and isinstance(m.get('content'), str) and m['content'] for m in messages), 'AI completion contains no assistant answer')
                self.done = data

    def finish(self):
        self.decoder.decode(b'', final=True)
        require(self.done_count == 1 and self.done is not None and bool(self.text), 'SSE closed without one complete answer')


class LocalAiWorkload:
    def prepare_local_ai(self):
        # Fail closed even when local is merely one selectable model but a paid default is active.
        for user in range(max(self.ai_texts, self.ai_images)):
            local_ai(self.call(user, '/api/ai/status', label='local-ai-preflight'))
        if self.ai_images:
            status = self.call(0, '/api/ai/image-generation/status', label='local-image-preflight')
            contract(status, {'enabled': 'bool', 'ready': 'bool', 'sizes': 'list'})
            require(status['enabled'] and status['ready'] and 768 in status['sizes']
                    and status.get('recipe') in ('flux2-klein-4b-v1', 'flux2-klein-4b-fp8-izo-v1'),
                    'local ComfyUI profile unavailable; external image fallback forbidden')

    def ai_text(self, user, barrier):
        request_id = str(uuid.uuid4())
        barrier.wait(timeout=60)
        thread = None
        started = time.monotonic()
        row = {'kind': 'text', 'user': user + 1, 'requestId': request_id,
               'ok': False, 'firstByteMs': None, 'firstTokenMs': None}
        completed = False
        try:
            thread = self.call(user, '/api/ai/threads', {}, expected=(201,), label='ai-thread')['id']
            require(isinstance(thread, str) and uuid.UUID(thread).version is not None and str(uuid.UUID(thread)) == thread, 'invalid own AI thread identity')
            row['threadId'] = thread
            with self.lock:
                self.inflight[f'ai-text:{user}'] = {'user': user + 1, 'kind': 'text', 'threadId': thread, 'requestId': request_id}
            self.checkpoint(True)
            # Revalidate immediately before this explicitly local request.
            local_ai(self.call(user, '/api/ai/status', label='local-ai-selection'))
            body = {'message': f'Учебная проверка {user+1}. В двух предложениях объясни переменную и вычисли {user+11}+7. Не используй инструменты.',
                    'requestId': request_id, 'model': 'ollama-local', 'webSearchMode': 'off',
                    'nativeImage': {'mode': 'off', 'size': 768}}
            headers = self.headers(user)
            headers['Content-Type'] = 'application/json'
            req = urllib.request.Request(self.origin + f'/api/ai/threads/{thread}/messages/stream',
                                         data=json.dumps(body).encode(), headers=headers)
            stream = SseDecoder(request_id)
            stream_started = time.monotonic()
            with self.opener.open(req, timeout=20) as response:
                require(response.status == 200 and response.headers.get('Content-Type', '').startswith('text/event-stream'), 'AI did not return SSE')
                while True:
                    require(time.monotonic() - started <= 180 and not self.cancel_event.is_set(), 'local AI deadline/cancellation')
                    chunk = response.read1(8192)
                    if not chunk:
                        break
                    if row['firstByteMs'] is None:
                        row['firstByteMs'] = round((time.monotonic() - stream_started) * 1000, 2)
                    previous = len(stream.text)
                    stream.feed(chunk)
                    if len(stream.text) > previous and row['firstTokenMs'] is None:
                        row['firstTokenMs'] = round((time.monotonic() - stream_started) * 1000, 2)
                stream.finish()
            completed = True
            history = self.call(user, f'/api/ai/threads/{thread}/messages', label='ai-text-reopen')
            contract(history, {'items': 'list'})
            answer = next((m for m in history['items'] if m.get('requestId') == request_id and m.get('role') == 'assistant'), None)
            require(answer is not None and answer.get('content') == stream.text, 'AI answer not preserved in history')
            row.update(ok=True, comments=stream.comments, doneCount=stream.done_count,
                       answerChars=len(stream.text), answerSha256=hashlib.sha256(stream.text.encode()).hexdigest())
        except Exception as e:
            row['error'] = str(e)[:160] if isinstance(e, ContractError) else type(e).__name__
            self.halt_new.set()
        finally:
            if not completed and thread is not None:
                try:
                    self.call(user, f'/api/ai/threads/{thread}/requests/{request_id}/stop', {}, label='ai-own-stop', cleanup=True)
                    row['ownStopAcknowledged'] = True
                except Exception:
                    row['ownStopAcknowledged'] = False
            row['ms'] = round((time.monotonic() - started) * 1000, 2)
            with self.lock:
                self.ai_results.append(row)
                self.inflight.pop(f'ai-text:{user}', None)
            self.checkpoint(True)
        return row

    def image_receipt(self, user, thread, request_id, states, started):
        path = f'/api/ai/threads/{thread}/image-generations/{request_id}'
        while time.monotonic() - started <= 240 and not self.cancel_event.is_set():
            job = self.call(user, path, label='image-poll', cleanup=True)
            contract(job, {'requestId': 'string', 'threadId': 'string', 'state': 'string', 'cancelRequested': 'bool'})
            require(job['requestId'] == request_id and job['threadId'] == thread, 'wrong image receipt')
            require(job['state'] in ('admitted', 'queued', 'submitting', 'unknown', 'running', 'storing', 'complete', 'failed', 'cancelled'), 'unknown image state')
            states.append({'state': job['state'], 'ms': round((time.monotonic() - started) * 1000, 2),
                           'queuePosition': job.get('queuePosition') if type(job.get('queuePosition')) is int else None})
            if job['state'] in ('complete', 'failed', 'cancelled'):
                return job
            self.cancel_event.wait(2)
        raise ContractError('image deadline/cancellation')

    def ai_image(self, user, barrier):
        request_id = str(uuid.uuid4())
        barrier.wait(timeout=60)
        thread = None
        started = time.monotonic()
        row = {'kind': 'image', 'user': user + 1, 'requestId': request_id, 'ok': False, 'states': []}
        active_id = request_id
        complete = False
        path = None
        prompt = f'Educational blue robot with pencil number {user+1}, white background, illustration, no text'
        def submit(rid, label):
            return self.call(user, path, {'requestId': rid, 'prompt': prompt, 'size': 768}, expected=(200, 201, 202), label=label)
        try:
            thread = self.call(user, '/api/ai/threads', {}, expected=(201,), label='image-thread')['id']
            require(isinstance(thread, str) and uuid.UUID(thread).version is not None and str(uuid.UUID(thread)) == thread, 'invalid own image thread identity')
            row['threadId'] = thread
            with self.lock:
                self.inflight[f'ai-image:{user}'] = {'user': user + 1, 'kind': 'image', 'threadId': thread, 'requestId': request_id}
            self.checkpoint(True)
            path = f'/api/ai/threads/{thread}/image-generations'
            local_ai(self.call(user, '/api/ai/status', label='local-image-selection'))
            first = submit(request_id, 'image-admit')
            require(first.get('requestId') == request_id and first.get('threadId') == thread, 'image admission identity mismatch')
            row['admitMs'] = round((time.monotonic() - started) * 1000, 2)
            # An explicit confirmed idempotency probe, not a retry of an uncertain POST.
            replay = submit(request_id, 'image-confirmed-idempotency-probe')
            require(replay.get('requestId') == request_id and replay.get('threadId') == thread, 'image replay changed identity')
            row['confirmedReplaySameRequestId'] = True
            if user == self.ai_images - 1:
                self.call(user, path + '/' + request_id + '/cancel', {}, label='image-own-cancel', cleanup=True)
                cancelled = self.image_receipt(user, thread, request_id, row['states'], started)
                require(cancelled['state'] == 'cancelled', 'image completed before cancel; cancellation NOT_VERIFIED')
                row['cancelledConfirmed'] = True
                # Only an explicitly confirmed cancellation starts a new operation.
                active_id = str(uuid.uuid4())
                with self.lock:
                    self.inflight[f'ai-image:{user}']['requestId'] = active_id
                self.checkpoint(True)
                retry = submit(active_id, 'image-retry-after-confirmed-cancel')
                require(retry.get('requestId') == active_id, 'image retry identity mismatch')
                row['retryRequestId'] = active_id
            job = self.image_receipt(user, thread, active_id, row['states'], started)
            require(job['state'] == 'complete', 'admitted image did not complete')
            descriptor = job.get('image', {})
            download = f'{path}/{active_id}/image'
            require(descriptor.get('downloadUrl') == download and descriptor.get('mimeType') == 'image/png', 'external/invalid image download URL')
            binary = self.call(user, download, label='image-read', cleanup=True)
            width, height = png_dimensions(binary)
            require((width, height) == (768, 768) == (descriptor['width'], descriptor['height']) and len(binary) == descriptor['sizeBytes'], 'image dimensions/size differ from requested 768 square')
            again = self.call(user, download, label='image-reopen', cleanup=True)
            require(binary == again, 'stored image changed on reopen')
            complete = True
            row.update(ok=True, bytes=len(binary), width=width, height=height, sha256=hashlib.sha256(binary).hexdigest())
        except Exception as e:
            row['error'] = str(e)[:160] if isinstance(e, ContractError) else type(e).__name__
            self.halt_new.set()
        finally:
            if not complete and path is not None:
                try:
                    self.call(user, path + '/' + active_id + '/cancel', {}, label='image-own-cancel-cleanup', cleanup=True)
                    row['ownCancelAcknowledged'] = True
                except Exception:
                    row['ownCancelAcknowledged'] = False
            row['ms'] = round((time.monotonic() - started) * 1000, 2)
            with self.lock:
                self.ai_results.append(row)
                self.inflight.pop(f'ai-image:{user}', None)
            self.checkpoint(True)
        return row
