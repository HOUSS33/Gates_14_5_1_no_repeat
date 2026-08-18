"""
========================================================================================
BOT DE SIGNAL LIVE (DOZEN/COLUMN) : WEBSOCKET -> MACHINE A ETATS -> TELEGRAM
========================================================================================
Même infrastructure WebSocket/Telegram/CSV que le bot ODD/EVEN, mais moteur de
signal = logique DOZEN/COLUMN "streak de CHAOS" (pas répétition) — identique
au backtester "Coupe-Nuit" :

- streak = nombre de tirages consécutifs où la douzaine (ou colonne) CHANGE
  par rapport au tirage précédent. Une répétition (même douzaine 2x de suite,
  y compris deux 0 d'affilée) remet le streak à 0.
- Dès que streak atteint chaos_threshold (20), on parie sur la douzaine/colonne
  qui vient JUSTE de sortir (pas l'opposée — logique "chasse", pas "rattrapage").
- Mise fixe sur 5 paliers : 55, 55, 110, 165, 275 DHS.
- Sur une perte : on change la cible pour chasser la douzaine/colonne qui vient
  de sortir. Un 0 en phase d'attaque compte comme une perte (consomme un
  palier) mais NE change PAS la cible.
- Un signal ne se déclenche JAMAIS sur un 0.
- Bust = perte des 5 paliers d'affilée -> recharge automatique du capital.
========================================================================================
"""

import json
import time
import csv
import os
import requests
import websocket  # pip install websocket-client
from datetime import datetime

# ==========================================================================
# 0. ENREGISTREMENT CSV
# ==========================================================================
VOLUME_PATH = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
CSV_FILE = os.path.join(VOLUME_PATH, "roulette_data.csv")
CSV_HEADERS = ["Timestamp", "GameID", "Result", "Color"]


def load_last_game_id_from_csv():
    """Lit le dernier gameId déjà enregistré, pour rattraper les spins
    manqués pendant une coupure/redémarrage (AVANT de recréer le header)."""
    if not os.path.exists(CSV_FILE):
        return None
    try:
        with open(CSV_FILE, newline='') as f:
            rows = list(csv.reader(f))
        if len(rows) <= 1:
            return None
        return rows[-1][1]
    except Exception as e:
        print(f"[CSV] Impossible de lire le dernier gameId : {e}")
        return None


if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, mode='w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)


def log_spin_to_csv(game_id, result, color, spin_time=None):
    timestamp = spin_time if spin_time else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [timestamp, game_id, result, color]
    with open(CSV_FILE, mode='a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(row)

# ==========================================================================
# 1. CONFIGURATION TELEGRAM
# ==========================================================================
TELEGRAM_BOT_TOKEN = "8672585271:AAF0jvfTLSGejR6-Fn3tWzRrDlCmtNw2v94"
TELEGRAM_CHAT_ID = "6098394153"

def send_telegram_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Telegram] Erreur d'envoi : {e}")


# ==========================================================================
# 1bis. FILTRE TELEGRAM : ne relaie que les 2 signaux suivant un BUST
# ==========================================================================
class TelegramGate:
    """
    - Un message 🚨 (bust OU fin de séquence par épuisement des paliers) est
      TOUJOURS relayé vers Telegram, et arme les DEUX prochains signaux
      complets (⚡ ... jusqu'à leur ✅/🚨 de fin) pour relais intégral.
    - Tant que 'signals_to_relay' > 0, TOUT (⏳ alerte précoce, ⚡ signal,
      🟢 gain, ✅/🚨 fin) est relayé.
    - Dès que le 2e signal armé se termine, le compteur retombe à 0 et le
      bot redevient silencieux sur Telegram jusqu'au prochain 🚨.
    - Avant le tout premier bust de la session : silence total sur Telegram
      (à part le message de démarrage du bot).
    """
    def __init__(self):
        self.signals_to_relay = 0

    def should_relay(self, msg: str) -> bool:
        is_bust = msg.startswith("🚨")
        is_completion = msg.startswith("✅") or is_bust

        if is_bust:
            self.signals_to_relay = 2
            return True

        if self.signals_to_relay > 0:
            if is_completion:
                self.signals_to_relay -= 1
            return True

        return False


telegram_gate = TelegramGate()


# ==========================================================================
# 2. CONFIGURATION WEBSOCKET + PROXY RÉSIDENTIEL
# ==========================================================================
WS_URL = "wss://dga.pragmaticplaylive.net/ws"

TABLE_KEY = "2244"
CURRENCY = "EUR"
CASINO_ID = "il9srgw4dna22222"

WS_HEADERS = [
    "Accept-Encoding: gzip, deflate, br, zstd",
    "Accept-Language: en-US,en;q=0.9,fr-MA;q=0.8,fr;q=0.7",
    "Cache-Control: no-cache",
    "Pragma: no-cache",
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
]
WS_ORIGIN = "https://www.bigwinboard.com"

PROXY_HOST = "gw.dataimpulse.com"
PROXY_PORT = 824
PROXY_TYPE = "socks5"
PROXY_LOGIN = "c28464d2322ae2cb5a09"
PROXY_PASSWORD = "afd6703a49960bd1"
USE_PROXY = True

DEBUG_TRACE = False


# ==========================================================================
# 3. LA MACHINE A ETATS "EN LIGNE" — DOZEN/COLUMN, PALIERS FIXES
# ==========================================================================
def get_dozen(n):
    if 1 <= n <= 12: return 1
    if 13 <= n <= 24: return 2
    if 25 <= n <= 36: return 3
    return 0


def get_column(n):
    if n == 0: return 0
    if n % 3 == 1: return 1
    if n % 3 == 2: return 2
    if n % 3 == 0: return 3
    return 0


class LiveSignalEngine:
    def __init__(self, initial_capital, chaos_threshold=14, target_wins=1,
                 bet_ladder=(1, 1, 2, 3, 5)):
        self.bet_ladder = bet_ladder
        self.lives = len(bet_ladder)
        self.actual_required_capital = sum(bet_ladder)  # exposition max par séquence

        self.chaos_threshold = chaos_threshold
        self.target_wins = target_wins

        self.capital = initial_capital
        self.initial_capital = initial_capital
        self.total_real_deposits = initial_capital

        self.is_betting = False
        self.life_index = 0
        self.current_mode = None       # 'dozen' ou 'column'
        self.target_value = None
        self.wins_in_current_signal = 0
        self.current_sequence_loss = 0

        self.streak_dozen = 0
        self.streak_column = 0

        self.p_doz = None
        self.p_col = None

        self.signal_counter = 0

    def process_spin(self, number):
        events = []
        c_doz = get_dozen(number)
        c_col = get_column(number)

        if self.p_doz is None:
            self.p_doz, self.p_col = c_doz, c_col
            return events

        # -------------------------------------------------------------
        # PHASE DE GUET
        # -------------------------------------------------------------
        if not self.is_betting:
            self.streak_dozen = self.streak_dozen + 1 if c_doz != self.p_doz else 0
            self.streak_column = self.streak_column + 1 if c_col != self.p_col else 0

            # 🔔 ALERTE PRÉCOCE : une ligne avant le vrai signal. Purement
            # informative — aucune mise n'est engagée ici.
            if self.streak_dozen == self.chaos_threshold - 1 and c_doz != 0:
                events.append(
                    f"⏳ <b>ALERTE PRÉCOCE (DOZEN)</b> — streak à "
                    f"{self.streak_dozen}/{self.chaos_threshold}. Prépare-toi."
                )
            if self.streak_column == self.chaos_threshold - 1 and c_col != 0:
                events.append(
                    f"⏳ <b>ALERTE PRÉCOCE (COLUMN)</b> — streak à "
                    f"{self.streak_column}/{self.chaos_threshold}. Prépare-toi."
                )

            if self.streak_dozen >= self.chaos_threshold and c_doz != 0:
                self._trigger_signal('dozen', c_doz, events)
            elif self.streak_column >= self.chaos_threshold and c_col != 0:
                self._trigger_signal('column', c_col, events)

            self.p_doz, self.p_col = c_doz, c_col
            return events

        # -------------------------------------------------------------
        # PHASE D'ATTAQUE
        # -------------------------------------------------------------
        bet_amount = self.bet_ladder[self.life_index]
        actual_val = c_doz if self.current_mode == 'dozen' else c_col

        if actual_val == self.target_value and actual_val != 0:
            net_gain = bet_amount * 2
            self.capital += net_gain
            profit = net_gain - (self.current_sequence_loss + bet_amount)

            self.wins_in_current_signal += 1
            events.append(
                f"🟢 <b>GAIN</b> — Séquence #{self.signal_counter} | Palier {self.life_index + 1}\n"
                f"Profit : +{profit} DHS | Capital : {self.capital} DHS"
            )

            self.life_index = 0
            self.current_sequence_loss = 0

            if self.wins_in_current_signal >= self.target_wins:
                self.is_betting = False
                events.append(f"✅ Signal #{self.signal_counter} terminé — objectif atteint.")
        else:
            self.capital -= bet_amount
            self.current_sequence_loss += bet_amount
            self.life_index += 1
            if actual_val != 0:
                self.target_value = actual_val  # on chasse la valeur qui vient de sortir

            next_bet = self.bet_ladder[self.life_index] if self.life_index < self.lives else "BUST"
            label = self.current_mode.upper()
            events.append(
                f"🔴 Perte palier {self.life_index} ({label} #{self.target_value}) "
                f"| Prochaine mise : {next_bet} DHS"
            )

        if self.life_index >= self.lives:
            solde_restant = self.capital
            if solde_restant < self.actual_required_capital:
                apport = self.initial_capital - solde_restant
                self.total_real_deposits += apport
                self.capital = self.initial_capital
                events.append(
                    f"🚨 <b>BUST</b> — Recharge de {apport} DHS nécessaire. "
                    f"Capital remis à {self.initial_capital} DHS."
                )
            else:
                events.append(f"🚨 Fin de séquence — capital auto-suffisant ({solde_restant} DHS).")

            self.is_betting = False
            self.life_index = 0
            self.current_sequence_loss = 0
            self.streak_dozen = 0
            self.streak_column = 0

        self.p_doz, self.p_col = c_doz, c_col
        return events

    def _trigger_signal(self, mode, value, events):
        self.current_mode = mode
        self.target_value = value
        self.is_betting = True
        self.life_index = 0
        self.wins_in_current_signal = 0
        self.streak_dozen = 0
        self.streak_column = 0
        self.current_sequence_loss = 0
        self.signal_counter += 1

        label = f"{mode.upper()} #{value}"
        events.append(
            f"⚡ <b>SIGNAL #{self.signal_counter}</b> — {label}\n"
            f"Mise à placer : <b>{self.bet_ladder[0]} DHS</b> sur {label}"
        )


# ==========================================================================
# 4. CLIENT WEBSOCKET
# ==========================================================================
engine = LiveSignalEngine(
    initial_capital=12,   # couvre l'exposition max de la ladder (1+1+2+3+5)
    chaos_threshold=14,
    target_wins=1,
    bet_ladder=(1, 1, 2, 3, 5),
)

last_game_id = load_last_game_id_from_csv()
if last_game_id:
    print(f"[Rattrapage] Dernier gameId connu au démarrage : {last_game_id}")


def handle_new_result(number, table_id):
    t0 = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Nouveau spin (table {table_id}) : {number}")
    events = engine.process_spin(number)
    t1 = time.time()
    for msg in events:
        print(msg)  # toujours visible en console/logs, peu importe Telegram
        if telegram_gate.should_relay(msg):
            send_telegram_alert(msg)
    t2 = time.time()
    print(f"[TIMING] engine={t1-t0:.3f}s | telegram={t2-t1:.3f}s | total={t2-t0:.3f}s")


def process_new_results(results):
    global last_game_id

    if last_game_id is None:
        new_entries = list(reversed(results))
    else:
        idx = next((i for i, r in enumerate(results) if r.get("gameId") == last_game_id), None)
        if idx is None:
            print("[Rattrapage] ⚠️ Coupure trop longue (>20 spins) — rattrapage partiel impossible, reprise au plus récent.")
            new_entries = [results[0]] if results else []
        elif idx == 0:
            new_entries = []
        else:
            new_entries = list(reversed(results[:idx]))
            if len(new_entries) > 1:
                print(f"[Rattrapage] {len(new_entries)} spin(s) manqué(s) détecté(s), traitement en cours...")

    for entry in new_entries:
        game_id = entry.get("gameId")
        if game_id is None:
            continue

        last_game_id = game_id

        t_csv0 = time.time()
        log_spin_to_csv(game_id, entry.get("result"), entry.get("color"), entry.get("time"))
        t_csv1 = time.time()
        if t_csv1 - t_csv0 > 0.1:
            print(f"[TIMING] écriture CSV lente : {t_csv1 - t_csv0:.3f}s")

        try:
            number = int(entry["result"])
        except (KeyError, ValueError, TypeError):
            continue

        handle_new_result(number, TABLE_KEY)


def on_message(ws, message):
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return

    if str(data.get("tableId")) != TABLE_KEY:
        return

    results = data.get("last20Results")
    if not results:
        return

    process_new_results(results)


def on_error(ws, error):
    print(f"[WS] Erreur : {repr(error)} (type: {type(error).__name__})")


def on_close(ws, close_status_code, close_msg):
    print(f"[WS] Connexion fermée (code={close_status_code}, msg={close_msg}). Reconnexion dans 3s...")


def on_open(ws):
    print("[WS] Connexion établie.")
    send_telegram_alert(
        f"🎲 Bot DOZEN/COLUMN démarré (WebSocket). Capital : {engine.initial_capital} DHS, "
        f"seuil : {engine.chaos_threshold}, paliers : {engine.bet_ladder}.\n"
        f"Telegram : silencieux jusqu'au prochain BUST, puis relaie les 2 signaux suivants."
    )

    msg1 = json.dumps({"type": "available", "casinoId": CASINO_ID})
    ws.send(msg1)
    print(f"[WS] Message 'available' envoyé : {msg1}")

    time.sleep(1)

    msg2 = json.dumps({"type": "subscribe", "currency": CURRENCY, "key": TABLE_KEY, "casinoId": CASINO_ID})
    ws.send(msg2)
    print(f"[WS] Message 'subscribe' envoyé : {msg2}")


def run_forever_with_reconnect():
    if DEBUG_TRACE:
        import logging
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(message)s')
        websocket.enableTrace(True)

    while True:
        ws = websocket.WebSocketApp(
            WS_URL,
            header=WS_HEADERS,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        run_kwargs = {"ping_interval": 30, "ping_timeout": 25, "origin": WS_ORIGIN}
        if USE_PROXY:
            run_kwargs.update({
                "http_proxy_host": PROXY_HOST,
                "http_proxy_port": PROXY_PORT,
                "http_proxy_auth": (PROXY_LOGIN, PROXY_PASSWORD),
                "proxy_type": PROXY_TYPE,
            })

        try:
            ws.run_forever(**run_kwargs)
        except Exception as e:
            print(f"[WS] Exception : {repr(e)} (type: {type(e).__name__})")

        print("[WS] Reconnexion dans 3 secondes...")
        time.sleep(3)


if __name__ == "__main__":
    if VOLUME_PATH == ".":
        print(f"⚠️ ATTENTION : RAILWAY_VOLUME_MOUNT_PATH non détecté — CSV éphémère utilisé : {os.path.abspath(CSV_FILE)}")
    else:
        print(f"✅ Volume détecté ({VOLUME_PATH}) — CSV persistant utilisé : {CSV_FILE}")
    print(f"🎲 Bot DOZEN/COLUMN démarré | Paliers : {engine.bet_ladder} | Exposition max/signal : {engine.actual_required_capital} DHS")
    run_forever_with_reconnect()
