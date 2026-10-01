"""
Bot de avisos da Luminosity (CS2) por e-mail (Gmail), usando dados do HLTV.

Avisos:
  1. Jogo novo marcado (e mudança de horário)
  2. Lembretes antes do jogo (padrão: 1 dia antes e 60 min antes)
  3. Resultado final
  4. Novo evento/campeonato

Roda a cada X minutos (GitHub Actions) e guarda o que já avisou em state.json.
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

# ----------------------------- Configuração ---------------------------------
TEAM_ID = os.getenv("TEAM_ID", "6290")
TEAM_SLUG = os.getenv("TEAM_SLUG", "luminosity")
TEAM_NAME = os.getenv("TEAM_NAME", "Luminosity")
TEAM_URL = f"https://www.hltv.org/team/{TEAM_ID}/{TEAM_SLUG}"
HLTV = "https://www.hltv.org"

# Lembretes em minutos antes do jogo (padrão: 1 dia antes e 1 hora antes)
REMINDERS = sorted({int(x) for x in os.getenv("REMINDERS", "1440,60").split(",") if x.strip()},
                   reverse=True)
TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Sao_Paulo"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"  # 1 = só imprime, não envia

# E-mail (Gmail + senha de app)
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
# Destinatários separados por vírgula (vão em cópia oculta)
EMAIL_TO = [e.strip() for e in re.split(r"[,;\s]+", os.getenv("EMAIL_TO", "")) if e.strip()]

DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


# ----------------------------- HLTV -----------------------------------------
def fetch_team_page() -> str:
    """Baixa a página do time imitando um Chrome real (o HLTV usa Cloudflare)."""
    from curl_cffi import requests as creq

    last_err = None
    for attempt in range(3):
        try:
            r = creq.get(TEAM_URL, impersonate="chrome", timeout=30,
                         headers={"Accept-Language": "en-US,en;q=0.9"})
            if r.status_code == 200 and "matchesBox" in r.text:
                return r.text
            last_err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Não consegui acessar o HLTV ({last_err}).")


def _match_id(href: str):
    m = re.search(r"/matches/(\d+)/", href or "")
    return m.group(1) if m else None


def _event_id(href: str):
    m = re.search(r"/events/(\d+)/", href or "")
    return m.group(1) if m else None


def _parse_match_table(table) -> list[dict]:
    matches = []
    event_name = ""
    for row in table.find_all("tr"):
        classes = row.get("class") or []
        if "event-header-cell" in classes:
            a = row.find("a")
            event_name = a.get_text(strip=True) if a else row.get_text(strip=True)
            continue
        if "team-row" not in classes:
            continue
        link = row.select_one("a[href*='/matches/']")
        mid = _match_id(link["href"]) if link else None
        if not mid:
            continue
        date_span = row.select_one("td.date-cell [data-unix]")
        unix = int(date_span["data-unix"]) // 1000 if date_span else None
        t1 = row.select_one(".team-1")
        t2 = row.select_one(".team-2")
        scores = [s.get_text(strip=True) for s in row.select(".score-cell .score")]
        matches.append({
            "id": mid,
            "time": unix,
            "team1": t1.get_text(strip=True) if t1 else "?",
            "team2": t2.get_text(strip=True) if t2 else "?",
            "score1": scores[0] if len(scores) > 0 else "-",
            "score2": scores[1] if len(scores) > 1 else "-",
            "event": event_name,
            "url": HLTV + link["href"],
        })
    return matches


def parse_team_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one("#matchesBox")
    if box is None:
        raise RuntimeError("Estrutura do HLTV mudou: #matchesBox não encontrado.")

    upcoming, results = [], []
    for h2 in box.find_all("h2"):
        title = h2.get_text(strip=True).lower()
        table = h2.find_next("table")
        if table is None:
            continue
        if title.startswith("upcoming matches"):
            upcoming = _parse_match_table(table)
        elif title.startswith("recent results"):
            results = _parse_match_table(table)

    events = []
    ongoing = soup.select_one("#ongoingEvents")
    if ongoing:
        for a in ongoing.select("a[href*='/events/']"):
            eid = _event_id(a["href"])
            if not eid:
                continue
            name = a.select_one(".eventbox-eventname")
            dates = [int(s["data-unix"]) // 1000 for s in a.select(".eventbox-date [data-unix]")]
            events.append({
                "id": eid,
                "name": name.get_text(strip=True) if name else a.get_text(strip=True),
                "start": dates[0] if dates else None,
                "end": dates[1] if len(dates) > 1 else (dates[0] if dates else None),
                "url": HLTV + a["href"],
            })
    return {"upcoming": upcoming, "results": results, "events": events}


# ----------------------------- Formatação -----------------------------------
def fmt_dt(unix) -> str:
    if not unix:
        return "horário a definir"
    d = datetime.fromtimestamp(unix, TZ)
    return f"{DIAS[d.weekday()]} {d:%d/%m} às {d:%H:%M}"


def fmt_day(unix) -> str:
    return datetime.fromtimestamp(unix, TZ).strftime("%d/%m") if unix else "?"


def opponent(m: dict) -> str:
    return m["team2"] if m["team1"].lower() == TEAM_NAME.lower() else m["team1"]


def msg_new_match(m):
    return (f"🆕 *Jogo marcado: {TEAM_NAME} x {opponent(m)}*\n"
            f"🏆 {m['event']}\n"
            f"🗓️ {fmt_dt(m['time'])} (Brasília)\n{m['url']}")


def msg_time_changed(m, old):
    return (f"⏰ *Horário alterado: {TEAM_NAME} x {opponent(m)}*\n"
            f"Antes: {fmt_dt(old)}\nAgora: *{fmt_dt(m['time'])}* (Brasília)\n{m['url']}")


def _when(unix, now) -> str:
    """'hoje às 15:00', 'amanhã às 15:00' ou 'sex 02/10 às 15:00'."""
    d = datetime.fromtimestamp(unix, TZ)
    diff = (d.date() - datetime.fromtimestamp(now, TZ).date()).days
    if diff == 0:
        return f"hoje às {d:%H:%M}"
    if diff == 1:
        return f"amanhã às {d:%H:%M}"
    return fmt_dt(unix)


def msg_reminder(m, minutes_left, now=None):
    now = now or int(time.time())
    vs = f"{TEAM_NAME} x {opponent(m)}"
    if minutes_left >= 120:  # lembrete com antecedência (ex.: 1 dia antes)
        when = _when(m["time"], now)
        head = f"📅 *{when[0].upper() + when[1:]}: {vs}*"
    else:
        head = f"🔔 *Em {minutes_left} min: {vs}*"
    return (f"{head}\n"
            f"🏆 {m['event']}\n"
            f"Começa {fmt_dt(m['time'])} (Brasília)\n{m['url']}")


def msg_result(m):
    ours_first = m["team1"].lower() == TEAM_NAME.lower()
    our, their = (m["score1"], m["score2"]) if ours_first else (m["score2"], m["score1"])
    try:
        head = "✅ Vitória" if int(our) > int(their) else "❌ Derrota"
    except ValueError:
        head = "🏁 Fim de jogo"
    return (f"{head}: *{TEAM_NAME} {our} x {their} {opponent(m)}*\n"
            f"🏆 {m['event']}\n{m['url']}")


def msg_new_event(e):
    return (f"🏆 *Novo campeonato: {e['name']}*\n"
            f"📅 {fmt_day(e['start'])} a {fmt_day(e['end'])}\n{e['url']}")


# ----------------------------- E-mail ---------------------------------------
def _split_message(text: str) -> tuple[str, str]:
    """1ª linha vira o assunto; o resto, o corpo."""
    lines = text.strip().splitlines()
    subject = lines[0].replace("*", "").strip()
    return subject, "\n".join(lines[1:]).strip()


def _to_html(subject: str, body: str) -> str:
    from html import escape

    parts = []
    for line in body.splitlines():
        line = escape(line)
        line = re.sub(r"\*(.+?)\*", r"<b>\1</b>", line)
        line = re.sub(r"(https?://\S+)", r'<a href="\1">Ver no HLTV</a>', line)
        parts.append(line)
    title = escape(subject)
    return (f'<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.5">'
            f'<h2 style="margin:0 0 8px;color:#5b2a86">{title}</h2>'
            + "<br>".join(parts) +
            '<p style="color:#888;font-size:12px;margin-top:16px">Bot de avisos da Luminosity · dados do HLTV</p></div>')


def send_email(text: str) -> None:
    subject, body = _split_message(text)
    if DRY_RUN:
        print(f"---- [DRY_RUN] {subject} ----\n{body}\n")
        return
    import smtplib
    from email.message import EmailMessage

    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        raise RuntimeError("Faltam os segredos GMAIL_USER ou GMAIL_APP_PASSWORD.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Bot Luminosity <{GMAIL_USER}>"
    msg["To"] = GMAIL_USER          # você recebe como destinatário principal
    if EMAIL_TO:                      # amigos em cópia oculta (opcional)
        msg["Bcc"] = ", ".join(EMAIL_TO)
    msg.set_content(body)
    msg.add_alternative(_to_html(subject, body), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"E-mail enviado: {subject}")


# ----------------------------- Estado ---------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


# ----------------------------- Lógica ---------------------------------------
def compute_notifications(data: dict, state: dict, now: int) -> tuple[list[str], dict]:
    """Compara o que está no HLTV com o estado salvo. Retorna (mensagens, novo_estado)."""
    first_run = not state.get("initialized")
    matches = state.get("matches", {})   # id -> {time, sent (lembretes enviados), result_sent}
    events = state.get("events", {})     # id -> {name}
    out: list[str] = []

    def mark_passed(rec, secs_left):
        """Marca como enviados os lembretes cujo prazo já passou (evita aviso repetido
        logo depois de 'jogo marcado' ou 'horário alterado', que já trazem o horário)."""
        rec["sent"] = [t for t in REMINDERS if secs_left <= t * 60]

    # 1) Jogos futuros: novo jogo / horário alterado / lembretes
    for m in data["upcoming"]:
        rec = matches.get(m["id"])
        if rec is not None and "sent" not in rec:  # estado da versão antiga
            rec["sent"] = list(REMINDERS) if rec.pop("reminded", False) else []
        if rec is None:
            rec = {"time": m["time"], "sent": [], "result_sent": False}
            matches[m["id"]] = rec
            if m["time"]:
                mark_passed(rec, m["time"] - now)
            if not first_run:
                out.append(msg_new_match(m))
            continue
        if m["time"] and rec.get("time") and m["time"] != rec["time"]:
            out.append(msg_time_changed(m, rec["time"]))
            rec["time"] = m["time"]
            mark_passed(rec, m["time"] - now)
            continue
        if m["time"] and not rec.get("time"):
            rec["time"] = m["time"]
            mark_passed(rec, m["time"] - now)
            continue
        if not m["time"]:
            continue

        secs_left = m["time"] - now
        due = [t for t in REMINDERS if secs_left <= t * 60 and t not in rec["sent"]]
        if not due:
            continue
        rec["sent"] = sorted(set(rec["sent"]) | set(due), reverse=True)
        # Se mais de um venceu ao mesmo tempo (ex.: bot ficou parado), manda só o mais próximo.
        if secs_left > 0 and not first_run:
            out.append(msg_reminder(m, max(1, round(secs_left / 60)), now))

    # 2) Resultados: avisa uma vez por partida (só as recentes, até 24h)
    for m in data["results"]:
        if m["score1"] in ("-", "") or m["score2"] in ("-", ""):
            continue
        rec = matches.setdefault(m["id"], {"time": m["time"], "sent": list(REMINDERS), "result_sent": False})
        if rec.get("result_sent"):
            continue
        rec["result_sent"] = True
        recent = m["time"] and now - m["time"] < 24 * 3600
        if not first_run and recent:
            out.append(msg_result(m))

    # 3) Eventos/campeonatos novos
    for e in data["events"]:
        if e["id"] not in events:
            events[e["id"]] = {"name": e["name"]}
            if not first_run:
                out.append(msg_new_event(e))

    # Limpeza: esquece partidas com mais de 30 dias
    cutoff = now - 30 * 24 * 3600
    matches = {k: v for k, v in matches.items() if not v.get("time") or v["time"] > cutoff}

    new_state = {"initialized": True, "matches": matches, "events": events}
    return out, new_state


def main() -> int:
    state = load_state()
    try:
        html = fetch_team_page()
    except RuntimeError as e:
        # Falha temporária no HLTV não deve “quebrar” o bot; tenta na próxima rodada.
        print(f"AVISO: {e}")
        return 0

    data = parse_team_page(html)
    print(f"HLTV: {len(data['upcoming'])} jogos futuros, {len(data['results'])} resultados, "
          f"{len(data['events'])} eventos.")

    first_run = not state.get("initialized")
    msgs, new_state = compute_notifications(data, state, int(time.time()))

    if first_run:
        print("Primeira execução: estado salvo sem enviar avisos (evita spam).")
        if not DRY_RUN and os.getenv("SEND_WELCOME", "1") == "1":
            nxt = sorted([m for m in data["upcoming"] if m["time"]], key=lambda m: m["time"])
            txt = "🤖 *Bot da Luminosity ativado!*\nVou avisar jogos marcados, lembretes, resultados e campeonatos."
            if nxt:
                txt += f"\n\nPróximo jogo: {TEAM_NAME} x {opponent(nxt[0])} — {fmt_dt(nxt[0]['time'])} (Brasília)"
            send_email(txt)

    for text in msgs:
        send_email(text)

    save_state(new_state)
    print(f"{len(msgs)} aviso(s) enviado(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
