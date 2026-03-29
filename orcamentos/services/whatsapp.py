from __future__ import annotations

import base64
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import traceback
from html import escape
from urllib.parse import quote
from pathlib import Path
from typing import Any

CHROMIUM_INSTALL_TIMEOUT_SECONDS = 600
CHROMIUM_INSTALL_MAX_RETRIES = 2
WHATSAPP_MODERN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)


class WhatsAppSessionManager:
    def __init__(
        self,
        profile_dir: Path,
        *,
        connect_timeout_seconds: int = 180,
        send_min_interval_seconds: float = 1.0,
        send_max_interval_seconds: float = 1.8,
        send_burst_size: int = 10,
        send_burst_pause_min_seconds: float = 6.0,
        send_burst_pause_max_seconds: float = 10.0,
    ) -> None:
        self._profile_dir = profile_dir
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._install_process: subprocess.Popen[str] | None = None
        self._send_requests: queue.Queue[dict[str, Any]] = queue.Queue()
        self._debug_logs_enabled = (os.getenv("WHATSAPP_DEBUG_LOGS") or "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._auto_install_chromium = (os.getenv("WHATSAPP_PLAYWRIGHT_AUTO_INSTALL_CHROMIUM") or "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._last_state_log: tuple[str, str, bool] | None = None
        self._last_heartbeat_log_at = 0.0
        self._connect_timeout_seconds = max(int(connect_timeout_seconds or 0), 30)
        self._send_min_interval_seconds = max(send_min_interval_seconds, 0.0)
        self._send_max_interval_seconds = max(send_max_interval_seconds, self._send_min_interval_seconds)
        self._send_burst_size = max(send_burst_size, 0)
        self._send_burst_pause_min_seconds = max(send_burst_pause_min_seconds, 0.0)
        self._send_burst_pause_max_seconds = max(
            send_burst_pause_max_seconds,
            self._send_burst_pause_min_seconds,
        )
        self._sent_messages_count = 0
        self._last_send_completed_at = 0.0
        self._state: dict[str, Any] = {
            "status": "disconnected",
            "message": "WhatsApp desconectado.",
            "qr_code": None,
            "running": False,
            "profile_dir": str(self._profile_dir),
            "session_persistent": True,
            "session_saved": self._profile_dir.exists(),
            "updated_at": time.time(),
        }
        self._log(
            "manager_initialized "
            f"profile_dir={self._profile_dir} "
            f"connect_timeout_seconds={self._connect_timeout_seconds} "
            f"auto_install_chromium={self._auto_install_chromium}"
        )

    def start(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                self._log("start_requested_ignored reason=worker_already_running")
                return
            self._stop_event.clear()
            self._state.update(
                {
                    "status": "connecting",
                    "message": "Iniciando sessao do WhatsApp Web...",
                    "qr_code": None,
                    "running": True,
                    "updated_at": time.time(),
                }
            )
            self._worker = threading.Thread(target=self._run, daemon=True, name="whatsapp-session")
            self._worker.start()
            self._log("start_requested worker_started=true")

    def stop(self) -> None:
        self._log("stop_requested")
        self._stop_event.set()
        with self._lock:
            install_process = self._install_process
        if install_process and install_process.poll() is None:
            try:
                install_process.terminate()
            except Exception:
                pass
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=5)
        self._set_disconnected("WhatsApp desconectado.")
        self._log("stop_completed")

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["running"] = bool(self._worker and self._worker.is_alive() and not self._stop_event.is_set())
            state["session_saved"] = self._profile_dir.exists()
            return state

    def _set_state(self, status: str, message: str, qr_code: str | None = None) -> None:
        with self._lock:
            self._state.update(
                {
                    "status": status,
                    "message": message,
                    "qr_code": qr_code,
                    "running": True,
                    "profile_dir": str(self._profile_dir),
                    "session_persistent": True,
                    "session_saved": self._profile_dir.exists(),
                    "updated_at": time.time(),
                }
            )
        self._log_state(status, message, has_qr=bool(qr_code))

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._state.update(
                {
                    "status": "error",
                    "message": message,
                    "qr_code": None,
                    "running": False,
                    "profile_dir": str(self._profile_dir),
                    "session_persistent": True,
                    "session_saved": self._profile_dir.exists(),
                    "updated_at": time.time(),
                }
            )
        self._log(f"state_error message={message}")

    def _set_disconnected(self, message: str) -> None:
        with self._lock:
            self._state.update(
                {
                    "status": "disconnected",
                    "message": message,
                    "qr_code": None,
                    "running": False,
                    "profile_dir": str(self._profile_dir),
                    "session_persistent": True,
                    "session_saved": self._profile_dir.exists(),
                    "updated_at": time.time(),
                }
            )
        self._log_state("disconnected", message, has_qr=False)

    def _install_chromium_if_needed(self) -> bool:
        command = [sys.executable, "-m", "playwright", "install", "chromium"]
        self._log(f"chromium_install_check command={' '.join(command)}")
        for attempt in range(1, CHROMIUM_INSTALL_MAX_RETRIES + 1):
            if self._stop_event.is_set():
                self._log("chromium_install_aborted reason=stop_event_set")
                return False

            self._set_state("connecting", f"Preparando Chromium do Playwright (tentativa {attempt})...")
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except Exception as exc:
                self._log(f"chromium_install_start_failed error={exc}")
                self._set_error(f"Falha ao iniciar instalacao do Chromium: {exc}")
                return False

            with self._lock:
                self._install_process = process

            started_at = time.time()
            while process.poll() is None:
                if self._stop_event.is_set():
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    return False
                if time.time() - started_at > CHROMIUM_INSTALL_TIMEOUT_SECONDS:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    break
                time.sleep(0.25)

            try:
                stdout, stderr = process.communicate(timeout=2)
            except Exception:
                stdout, stderr = "", ""
            finally:
                with self._lock:
                    self._install_process = None

            if process.returncode == 0:
                self._log(f"chromium_install_ok attempt={attempt}")
                return True
            if self._stop_event.is_set():
                self._log("chromium_install_aborted_after_process reason=stop_event_set")
                return False

            output = (stderr or stdout or "").strip()
            if output:
                output = output.splitlines()[-1].strip()
            if not output:
                output = "Falha ao instalar Chromium automaticamente."
            self._log(
                f"chromium_install_failed attempt={attempt} return_code={process.returncode} "
                f"output_tail={output[:240]}"
            )
            if attempt >= CHROMIUM_INSTALL_MAX_RETRIES:
                self._set_error(output)
                return False
            self._set_state("connecting", "Falha ao preparar Chromium. Tentando novamente...")

        return False

    def _run(self) -> None:
        self._log("worker_run_started")
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            self._log("playwright_import_failed")
            self._set_error("Playwright nao esta instalado. Rode: pip install playwright")
            return

        self._profile_dir.mkdir(parents=True, exist_ok=True)

        if self._auto_install_chromium:
            if not self._install_chromium_if_needed():
                self._log("worker_run_aborted reason=chromium_install_failed_or_aborted")
                return
        else:
            self._log("chromium_auto_install_skipped")

        browser_context = None

        try:
            self._set_state("connecting", "Abrindo WhatsApp Web...")
            connect_started_at = time.time()
            has_connected_once = False
            first_qr_logged = False
            last_preview_attempt_at = 0.0
            last_hint_attempt_at = 0.0
            with sync_playwright() as playwright:
                self._log("playwright_started")
                launch_started_at = time.time()
                browser_context = self._launch_context_with_fallback(playwright)
                self._log(
                    "browser_context_launched "
                    f"elapsed_seconds={time.time() - launch_started_at:.2f}"
                )
                page = self._open_fresh_whatsapp_page(browser_context)
                self._log("fresh_page_opened")
                goto_started_at = time.time()
                page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded", timeout=120_000)
                self._log(
                    "whatsapp_page_loaded_domcontentloaded "
                    f"elapsed_seconds={time.time() - goto_started_at:.2f}"
                )

                while not self._stop_event.is_set():
                    now = time.time()
                    if now - self._last_heartbeat_log_at >= 10:
                        self._last_heartbeat_log_at = now
                        try:
                            current_url = (page.url or "").strip()
                        except Exception:
                            current_url = ""
                        self._log(f"heartbeat status={self._state.get('status')} page_url={current_url[:180]}")
                    if page.is_closed():
                        self._log("page_closed_unexpectedly")
                        self._set_error("Navegador do WhatsApp foi encerrado inesperadamente.")
                        break
                    if self._is_browser_rejected(page):
                        self._log("browser_rejected_detected")
                        self._set_error(
                            "WhatsApp Web rejeitou o navegador automatizado. "
                            "Atualize o Google Chrome da maquina e tente novamente."
                        )
                        break
                    is_connected = self._is_connected(page)
                    if is_connected:
                        has_connected_once = True
                    if is_connected:
                        self._set_state("connected", "Conectado ao WhatsApp.")
                    else:
                        if (
                            not has_connected_once
                            and self._connect_timeout_seconds > 0
                            and (time.time() - connect_started_at) >= self._connect_timeout_seconds
                        ):
                            timeout_minutes = max(1, self._connect_timeout_seconds // 60)
                            self._set_disconnected(
                                f"Tempo limite de {timeout_minutes} minuto(s) para conectar. Sessao encerrada."
                            )
                            self._log("connect_timeout_reached")
                            break
                        qr_code = self._capture_qr_code(page)
                        if qr_code:
                            if not first_qr_logged:
                                first_qr_logged = True
                                self._log(
                                    "first_qr_available "
                                    f"elapsed_since_connect_seconds={time.time() - connect_started_at:.2f}"
                                )
                            self._log("qr_code_detected")
                            self._set_state("waiting_qr", "Escaneie o QR Code com seu celular.", qr_code=qr_code)
                        else:
                            preview = None
                            if (now - last_preview_attempt_at) >= 4:
                                last_preview_attempt_at = now
                                preview = self._capture_page_preview(page)
                            if preview:
                                self._log("page_preview_captured_without_qr")
                                self._set_state(
                                    "waiting_qr",
                                    "Aguardando QR Code. Se nao aparecer em instantes, clique em desconectar e tente novamente.",
                                    qr_code=preview,
                                )
                            else:
                                page_hint = None
                                if (now - last_hint_attempt_at) >= 4:
                                    last_hint_attempt_at = now
                                    page_hint = self._extract_page_hint(page)
                                if page_hint:
                                    self._log(f"page_hint_without_qr hint={page_hint[:200]}")
                                    if (
                                        "works with google chrome" in page_hint.lower()
                                        or "browser is not supported" in page_hint.lower()
                                        or "navegador nao e suportado" in page_hint.lower()
                                    ):
                                        self._log("page_hint_detected_browser_block")
                                        self._set_error(
                                            "WhatsApp Web bloqueou o navegador neste ambiente da Render. "
                                            "Use um servidor com Chrome dedicado ou outro provedor de envio."
                                        )
                                        break
                                    self._set_state(
                                        "waiting_qr",
                                        f"Aguardando QR Code. Tela atual: {page_hint[:150]}",
                                        qr_code=self._build_status_placeholder_image(page_hint),
                                    )
                                else:
                                    self._set_state("connecting", "Carregando WhatsApp Web...")
                    self._process_send_requests(page, is_connected=is_connected)
                    if is_connected:
                        # Sessao conectada: resposta mais rapida para envios enfileirados.
                        time.sleep(0.15)
                    else:
                        time.sleep(0.75)
        except Exception as exc:
            message = str(exc)
            self._log(f"worker_run_exception error={message}")
            self._log(traceback.format_exc())
            if "Executable doesn't exist" in message:
                message = "Falha ao preparar Chromium automaticamente. Tente iniciar novamente."
            self._set_error(message)
        finally:
            with self._lock:
                self._install_process = None
            self._fail_pending_send_requests("Sessao Playwright encerrada.")
            if browser_context is not None:
                try:
                    browser_context.close()
                except Exception:
                    pass
            if self._stop_event.is_set():
                self._set_disconnected("WhatsApp desconectado.")
            else:
                with self._lock:
                    if self._state.get("status") != "error":
                        self._state.update(
                            {
                                "running": False,
                                "updated_at": time.time(),
                            }
                        )
            self._log("worker_run_finished")

    def _launch_compatible_context(self, playwright):
        launch_args = [
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--no-first-run",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--no-zygote",
        ]
        no_sandbox_enabled = (os.getenv("WHATSAPP_PLAYWRIGHT_NO_SANDBOX") or "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if no_sandbox_enabled:
            launch_args.extend(
                [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ]
            )
        common_kwargs = {
            "user_data_dir": str(self._profile_dir),
            "headless": True,
            "args": launch_args,
            "user_agent": WHATSAPP_MODERN_USER_AGENT,
            "locale": "pt-BR",
            "timeout": 45_000,
        }

        use_chrome_channel_env = (os.getenv("WHATSAPP_PLAYWRIGHT_USE_CHROME_CHANNEL") or "").strip().lower()
        if use_chrome_channel_env in {"0", "false", "no", "off"}:
            use_chrome_channel = False
        elif use_chrome_channel_env in {"1", "true", "yes", "on"}:
            use_chrome_channel = True
        else:
            # Em Linux (Render), prioriza o Chromium do Playwright.
            use_chrome_channel = sys.platform.startswith("win")

        if use_chrome_channel:
            try:
                self._log("launch_context_try channel=chrome")
                return playwright.chromium.launch_persistent_context(
                    channel="chrome",
                    **common_kwargs,
                )
            except Exception:
                self._log("launch_context_channel_chrome_failed fallback=playwright_chromium")
                pass

        self._log("launch_context_try channel=playwright_chromium")
        return playwright.chromium.launch_persistent_context(**common_kwargs)

    def _launch_context_with_fallback(self, playwright):
        try:
            return self._launch_compatible_context(playwright)
        except Exception as exc:
            message = str(exc or "")
            if "Executable doesn't exist" in message and not self._auto_install_chromium:
                self._log("launch_context_missing_executable retry=install_chromium")
                if self._install_chromium_if_needed():
                    return self._launch_compatible_context(playwright)
            raise

    @staticmethod
    def _open_fresh_whatsapp_page(browser_context):
        for existing_page in list(browser_context.pages):
            try:
                url = (existing_page.url or "").strip().lower()
            except Exception:
                url = ""
            if "web.whatsapp.com" in url or url in {"", "about:blank"}:
                try:
                    existing_page.close()
                except Exception:
                    pass
        return browser_context.new_page()

    @staticmethod
    def _is_connected(page) -> bool:
        selectors = (
            "#side",
            "[data-testid='chat-list-search']",
            "button[aria-label*='Nova conversa']",
            "button[aria-label*='New chat']",
        )
        for selector in selectors:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _capture_qr_code(page) -> str | None:
        # 1) Tentativa direta em elementos de QR (canvas/img) para obter imagem limpa.
        qr_selectors = (
            "canvas[aria-label*='Scan']",
            "canvas[aria-label*='Escanear']",
            "div[data-ref] canvas",
            "[data-testid='qrcode'] canvas",
            "img[alt*='QR']",
            "img[alt*='Qr']",
            "img[src*='data:image']",
            "canvas",
            "img",
        )
        for selector in qr_selectors:
            data_url = WhatsAppSessionManager._capture_element_as_data_url(page, selector)
            if data_url:
                return data_url

        # 2) Fallback: captura do container onde geralmente o QR aparece.
        container_selectors = (
            "[data-testid='qrcode']",
            "div[data-ref]",
            "div[role='img']",
            "main",
            "body",
        )
        for selector in container_selectors:
            data_url = WhatsAppSessionManager._capture_element_as_data_url(page, selector)
            if data_url:
                return data_url

        return None

    @staticmethod
    def _capture_page_preview(page) -> str | None:
        try:
            png = page.screenshot(type="png", full_page=False)
        except Exception:
            return None
        if not png:
            return None
        encoded = base64.b64encode(png).decode("ascii")
        if len(encoded) < 256:
            return None
        return f"data:image/png;base64,{encoded}"

    @staticmethod
    def _extract_page_hint(page) -> str | None:
        try:
            body_text = page.locator("body").inner_text(timeout=1_500).strip()
        except Exception:
            return None
        if not body_text:
            return None
        compact = " ".join(body_text.split())
        return compact[:260]

    @staticmethod
    def _build_status_placeholder_image(message: str) -> str:
        safe_message = escape((message or "Aguardando WhatsApp Web...")[:220])
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' width='900' height='560'>"
            "<rect width='100%' height='100%' fill='#f5f0e6'/>"
            "<rect x='18' y='18' width='864' height='524' rx='16' fill='#fffef9' stroke='#d8c9af' stroke-width='2'/>"
            "<text x='40' y='80' fill='#3e3a33' font-size='30' font-family='Arial, sans-serif' font-weight='700'>WhatsApp Web (Render)</text>"
            f"<text x='40' y='130' fill='#625a4e' font-size='23' font-family='Arial, sans-serif'>{safe_message}</text>"
            "</svg>"
        )
        return f"data:image/svg+xml;charset=utf-8,{quote(svg)}"

    @staticmethod
    def _capture_element_as_data_url(page, selector: str) -> str | None:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 6)
        except Exception:
            return None

        for index in range(count):
            try:
                element = locator.nth(index)

                # Em canvas, tentar extrair o pixel data direto do elemento.
                try:
                    tag_name = element.evaluate("el => (el.tagName || '').toLowerCase()")
                except Exception:
                    tag_name = ""

                if tag_name == "canvas":
                    try:
                        canvas_data_url = element.evaluate("el => el.toDataURL('image/png')")
                    except Exception:
                        canvas_data_url = None
                    if isinstance(canvas_data_url, str) and canvas_data_url.startswith("data:image/png;base64,"):
                        if len(canvas_data_url) > 256:
                            return canvas_data_url

                # Em img, se ja houver data URL, reaproveita.
                if tag_name == "img":
                    try:
                        src = element.get_attribute("src") or ""
                    except Exception:
                        src = ""
                    if src.startswith("data:image/"):
                        return src

                box = element.bounding_box()
                if not box:
                    continue
                if box.get("width", 0) < 120 or box.get("height", 0) < 120:
                    continue

                png = element.screenshot(type="png")
                if not png:
                    continue
                encoded = base64.b64encode(png).decode("ascii")
                if len(encoded) < 256:
                    continue
                return f"data:image/png;base64,{encoded}"
            except Exception:
                continue

        return None

    @staticmethod
    def _is_browser_rejected(page) -> bool:
        markers = (
            "works with google chrome",
            "update google chrome",
            "navegador nao e suportado",
            "browser is not supported",
        )
        try:
            body_text = page.locator("body").inner_text(timeout=2_000).strip().lower()
        except Exception:
            return False
        return any(marker in body_text for marker in markers)

    def send_message_with_connected_session(self, phone: str, message: str) -> tuple[bool, str | None]:
        with self._lock:
            status = str(self._state.get("status") or "disconnected").strip().lower()

        if status != "connected":
            return False, "Sessao Playwright nao esta conectada ao WhatsApp."

        digits = re.sub(r"\D", "", phone or "")
        if not digits:
            return False, "Telefone invalido para envio no WhatsApp."

        text = (message or "").strip()
        if not text:
            return False, "Mensagem vazia."

        request_payload: dict[str, Any] = {
            "phone": digits,
            "message": text,
            "done": threading.Event(),
            "result": (False, "Falha desconhecida ao enfileirar envio."),
        }
        self._send_requests.put(request_payload)

        if not request_payload["done"].wait(timeout=120):
            return False, "Tempo limite excedido ao enviar mensagem pela sessao Playwright."

        result = request_payload.get("result")
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), result[1]
        return False, "Resposta invalida do worker de envio do Playwright."

    def _process_send_requests(self, page, *, is_connected: bool) -> None:
        while True:
            try:
                request_payload = self._send_requests.get_nowait()
            except queue.Empty:
                return

            try:
                if not is_connected:
                    request_payload["result"] = (False, "Sessao Playwright desconectada durante o envio.")
                else:
                    self._apply_send_pacing()
                    request_payload["result"] = self._send_message_in_context(
                        page,
                        request_payload.get("phone", ""),
                        request_payload.get("message", ""),
                    )
                    sent_ok = bool(request_payload["result"][0]) if isinstance(request_payload.get("result"), tuple) else False
                    if sent_ok:
                        self._sent_messages_count += 1
                        self._last_send_completed_at = time.time()
            except Exception as exc:
                request_payload["result"] = (False, str(exc))
            finally:
                done_event = request_payload.get("done")
                if isinstance(done_event, threading.Event):
                    done_event.set()

    def _apply_send_pacing(self) -> None:
        if self._send_min_interval_seconds > 0 or self._send_max_interval_seconds > 0:
            target_interval = random.uniform(
                self._send_min_interval_seconds,
                self._send_max_interval_seconds,
            )
            wait_seconds = (self._last_send_completed_at + target_interval) - time.time()
            if wait_seconds > 0:
                time.sleep(wait_seconds)

        if self._send_burst_size > 0 and self._sent_messages_count > 0:
            if self._sent_messages_count % self._send_burst_size == 0:
                burst_pause = random.uniform(
                    self._send_burst_pause_min_seconds,
                    self._send_burst_pause_max_seconds,
                )
                if burst_pause > 0:
                    time.sleep(burst_pause)

    def _fail_pending_send_requests(self, message: str) -> None:
        while True:
            try:
                request_payload = self._send_requests.get_nowait()
            except queue.Empty:
                return
            request_payload["result"] = (False, message)
            done_event = request_payload.get("done")
            if isinstance(done_event, threading.Event):
                done_event.set()

    def _send_message_in_context(self, page, phone: str, message: str) -> tuple[bool, str | None]:
        try:
            target_url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(message)}"
            self._log(f"send_message_start phone={phone} target_url={target_url[:220]}")
            page.goto(target_url, wait_until="domcontentloaded", timeout=120_000)

            if self._is_browser_rejected(page):
                return False, "WhatsApp Web rejeitou o navegador automatizado."

            if not self._wait_until_chat_ready(page):
                return False, "A conversa nao ficou pronta para envio no WhatsApp Web."

            send_clicked = self._click_send_button(page)
            if send_clicked:
                self._log("send_message_ok method=click_send_button")
                return True, None

            if self._type_and_send_with_composer(page, message):
                self._log("send_message_ok method=composer_enter")
                return True, None

            self._log("send_message_failed reason=unable_to_trigger_send")
            return False, "Nao foi possivel enviar a mensagem no WhatsApp Web."

        except Exception as exc:
            self._log(f"send_message_exception error={exc}")
            self._log(traceback.format_exc())
            return False, str(exc)

    def _log(self, message: str) -> None:
        if not self._debug_logs_enabled:
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"[WHATSAPP][{ts}] {message}", flush=True)

    def _log_state(self, status: str, message: str, *, has_qr: bool) -> None:
        signature = (status, message, has_qr)
        if self._last_state_log == signature:
            return
        self._last_state_log = signature
        self._log(f"state_update status={status} has_qr={has_qr} message={message}")

    def _wait_until_chat_ready(self, page, timeout_seconds: int = 35) -> bool:
        deadline = time.time() + max(timeout_seconds, 5)
        while time.time() < deadline:
            if self._is_browser_rejected(page):
                return False

            self._try_click_pre_chat_actions(page)
            if self._is_chat_ready(page):
                return True

            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
        return self._is_chat_ready(page)

    @staticmethod
    def _try_click_pre_chat_actions(page) -> None:
        action_selectors = (
            "button:has-text('Usar nesta janela')",
            "button:has-text('Use this window')",
            "button:has-text('Continuar para conversa')",
            "a:has-text('Continuar para conversa')",
            "button:has-text('Continue to Chat')",
            "a:has-text('Continue to Chat')",
            "button:has-text('Usar o WhatsApp Web')",
            "button:has-text('Use WhatsApp Web')",
            "a[href*='web.whatsapp.com/send']",
        )
        for selector in action_selectors:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                target = locator.first
                if not target.is_visible():
                    continue
                target.click(timeout=2_000)
                return
            except Exception:
                continue

    @staticmethod
    def _is_chat_ready(page) -> bool:
        ready_selectors = (
            "button[data-testid='compose-btn-send']",
            "button[aria-label='Enviar']",
            "button[aria-label='Send']",
            "footer div[contenteditable='true']",
            "div[role='textbox'][contenteditable='true']",
        )
        for selector in ready_selectors:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                if locator.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _click_send_button(page) -> bool:
        button_selectors = (
            "button[data-testid='compose-btn-send']",
            "button[aria-label='Enviar']",
            "button[aria-label='Send']",
            "span[data-icon='send']",
        )
        for selector in button_selectors:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                target = locator.first
                if selector.startswith("span["):
                    # Em algumas versoes, o icone fica dentro do botao.
                    target = target.locator("xpath=ancestor::button[1]")
                target.click(timeout=8_000)
                return True
            except Exception:
                continue

        return False

    @staticmethod
    def _type_and_send_with_composer(page, message: str) -> bool:
        composer_selectors = (
            "footer div[contenteditable='true']",
            "div[role='textbox'][contenteditable='true']",
        )
        for selector in composer_selectors:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                field = locator.last
                if not field.is_visible():
                    continue
                field.focus(timeout=3_000)
                try:
                    field.press("Control+A")
                    field.press("Backspace")
                except Exception:
                    pass
                try:
                    field.fill(message)
                except Exception:
                    page.keyboard.type(message, delay=8)
                field.press("Enter")
                return True
            except Exception:
                continue

        return False
