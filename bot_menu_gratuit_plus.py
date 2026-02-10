import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from cachetools import TTLCache

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN manquant dans .env")

CACHE = TTLCache(maxsize=2048, ttl=60)

# ==========
# ESPN (gratuit, sans clé)
# ==========
ESPN_BASE = "https://site.api.espn.com/apis/site/v2"

# Soccer: ligues demandées + compétitions européennes + international (codes ESPN courants)
SOCCER_LEAGUES = [
    ("eng.1", "Premier League"),
    ("esp.1", "LaLiga"),
    ("ita.1", "Serie A"),
    ("fra.1", "Ligue 1"),
    ("ger.1", "Bundesliga"),
    ("uefa.champions", "UEFA Champions League"),
    ("uefa.europa", "UEFA Europa League"),
    ("uefa.europa.conf", "UEFA Conference League"),
    ("fifa.world", "International (FIFA)"),
]

BASKET_LEAGUES = [
    ("nba", "NBA"),
    ("euroleague", "EuroLeague"),
    # Betclic Elite: ESPN ne la couvre pas toujours selon régions; on la laisse en option.
    ("france-lnb", "Betclic ÉLITE (si dispo)"),
]

SPORT_LABELS = {
    "soccer": "⚽ Foot",
    "basketball": "🏀 Basket",
    "tennis": "🎾 Tennis",
    "nhl": "🏒 NHL",
    "nfl": "🏈 NFL",
    "mlb": "⚾ MLB",
}

def fmt_dt(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d/%m %H:%M")
    except Exception:
        return iso_str or "??"

def yyyymmdd(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")

async def http_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    key = url + "|" + str(params or {})
    if key in CACHE:
        return CACHE[key]
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(url, params=params, headers={"User-Agent": "telegram-bot/1.0"})
        r.raise_for_status()
        data = r.json()
        CACHE[key] = data
        return data

def markets_for(sport: str) -> List[str]:
    if sport == "soccer":
        return ["1X2", "Double chance (1X/12/X2)", "Over/Under 2.5", "BTTS", "Handicap (indicatif)"]
    if sport == "basketball":
        return ["Moneyline", "Spread (indicatif)", "Total points (O/U)", "Mi-temps", "Team totals"]
    if sport == "tennis":
        return ["Vainqueur", "Nombre de sets", "O/U jeux", "Handicap jeux", "Vainqueur 1er set"]
    if sport == "nhl":
        return ["Moneyline", "Puck line", "Total buts (O/U)", "1er tiers", "Team totals"]
    if sport == "nfl":
        return ["Moneyline", "Spread", "Total points (O/U)", "Team totals", "Mi-temps"]
    if sport == "mlb":
        return ["Moneyline", "Run line", "Total runs (O/U)", "1st 5 innings", "Team totals"]
    return []

# =========================
# FORM (5 derniers matchs) via ESPN Team Schedule
# =========================
async def team_form_espn(sport_path: str, league: str, team_id: str) -> str:
    """
    sport_path: ex "soccer", "basketball", "football", "hockey", "baseball"
    league: ex "eng.1", "nba", "nfl", "nhl", "mlb"
    """
    try:
        url = f"{ESPN_BASE}/sports/{sport_path}/{league}/teams/{team_id}/schedule"
        data = await http_get_json(url, params={"limit": 25})
        events = data.get("events", []) or []
        # On prend les 5 derniers "completed"
        results: List[str] = []
        for ev in events:
            competitions = ev.get("competitions", []) or []
            if not competitions:
                continue
            comp = competitions[0]
            status = (comp.get("status", {}) or {}).get("type", {}) or {}
            if not status.get("completed"):
                continue
            comps = comp.get("competitors", []) or []
            if len(comps) < 2:
                continue

            # Qui est home/away
            c1, c2 = comps[0], comps[1]
            s1 = int((c1.get("score") or "0"))
            s2 = int((c2.get("score") or "0"))
            n1 = ((c1.get("team") or {}).get("abbreviation") or (c1.get("team") or {}).get("displayName") or "T1")
            n2 = ((c2.get("team") or {}).get("abbreviation") or (c2.get("team") or {}).get("displayName") or "T2")
            date = fmt_dt(comp.get("date", ev.get("date", "")))

            # Résultat pour team_id
            team1_id = str((c1.get("team") or {}).get("id", ""))
            team2_id = str((c2.get("team") or {}).get("id", ""))
            if team1_id == team_id:
                wl = "W" if s1 > s2 else ("L" if s1 < s2 else "D")
            elif team2_id == team_id:
                wl = "W" if s2 > s1 else ("L" if s2 < s1 else "D")
            else:
                continue

            results.append(f"{wl} {n1} {s1}-{s2} {n2} ({date})")
            if len(results) >= 5:
                break

        return "📈 Forme (5 derniers):\n- " + "\n- ".join(results) if results else "📈 Forme: indisponible"
    except Exception:
        return "📈 Forme: indisponible"

# =========================
# MATCH LIST (aujourd’hui / demain)
# =========================
async def espn_scoreboard(sport_path: str, league: str, dt: datetime) -> Dict[str, Any]:
    url = f"{ESPN_BASE}/sports/{sport_path}/{league}/scoreboard"
    return await http_get_json(url, params={"dates": yyyymmdd(dt), "limit": 300})

def parse_espn_events_to_matches(
    sport_key: str,
    scoreboard: Dict[str, Any],
    league_code: str,
    league_name: str,
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for ev in (scoreboard.get("events", []) or []):
        competitions = ev.get("competitions", []) or []
        if not competitions:
            continue
        comp = competitions[0]
        comps = comp.get("competitors", []) or []
        if len(comps) < 2:
            continue

        # ESPN fournit home/away via "homeAway"
        home_obj = next((c for c in comps if c.get("homeAway") == "home"), comps[0])
        away_obj = next((c for c in comps if c.get("homeAway") == "away"), comps[1])

        home_team = (home_obj.get("team") or {})
        away_team = (away_obj.get("team") or {})

        matches.append({
            "sport": sport_key,
            "league": league_code,
            "league_name": league_name,
            "id": str(ev.get("id")),
            "start_time": comp.get("date", ev.get("date", "")),
            "home": home_team.get("displayName") or home_team.get("shortDisplayName") or "HOME",
            "away": away_team.get("displayName") or away_team.get("shortDisplayName") or "AWAY",
            "home_id": str(home_team.get("id", "")),
            "away_id": str(away_team.get("id", "")),
        })
    return matches

async def fetch_soccer_matches(dt: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for code, name in SOCCER_LEAGUES:
        try:
            sb = await espn_scoreboard("soccer", code, dt)
            out.extend(parse_espn_events_to_matches("soccer", sb, code, name))
        except Exception:
            continue
    return out

async def fetch_basket_matches(dt: datetime, league_code: str, league_name: str) -> List[Dict[str, Any]]:
    try:
        sb = await espn_scoreboard("basketball", league_code, dt)
        return parse_espn_events_to_matches("basketball", sb, league_code, league_name)
    except Exception:
        return []

async def fetch_nfl_matches(dt: datetime) -> List[Dict[str, Any]]:
    sb = await espn_scoreboard("football", "nfl", dt)
    return parse_espn_events_to_matches("nfl", sb, "nfl", "NFL")

async def fetch_nhl_matches_espn(dt: datetime) -> List[Dict[str, Any]]:
    sb = await espn_scoreboard("hockey", "nhl", dt)
    return parse_espn_events_to_matches("nhl", sb, "nhl", "NHL")

async def fetch_mlb_matches_espn(dt: datetime) -> List[Dict[str, Any]]:
    sb = await espn_scoreboard("baseball", "mlb", dt)
    return parse_espn_events_to_matches("mlb", sb, "mlb", "MLB")

# Tennis via ESPN (ATP/WTA). (Si un des deux ne renvoie rien, on ignore.)
async def fetch_tennis_matches(dt: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for league_code, league_name in [("atp", "ATP"), ("wta", "WTA")]:
        try:
            sb = await espn_scoreboard("tennis", league_code, dt)
            out.extend(parse_espn_events_to_matches("tennis", sb, league_code, league_name))
        except Exception:
            continue
    return out

# =========================
# NHL goalies probable (API NHL)
# =========================
async def nhl_goalies(game_id: str) -> str:
    try:
        landing = await http_get_json(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/landing")
        goalies = (landing.get("matchup", {}) or {}).get("goalies", {}) or {}
        h = (goalies.get("home", {}) or {}).get("playerName")
        a = (goalies.get("away", {}) or {}).get("playerName")
        if not (h or a):
            return "🧤 Gardiens probables: indisponible"
        lines = ["🧤 Gardiens probables (si dispo):"]
        if a: lines.append(f"- Extérieur: {a}")
        if h: lines.append(f"- Domicile: {h}")
        return "\n".join(lines)
    except Exception:
        return "🧤 Gardiens probables: indisponible"

# =========================
# MLB pitchers probable (StatsAPI)
# =========================
async def mlb_pitchers(game_pk: str) -> str:
    try:
        data = await http_get_json(f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live")
        gd = data.get("gameData", {}) or {}
        pp = gd.get("probablePitchers", {}) or {}
        hp = (pp.get("home", {}) or {}).get("fullName")
        ap = (pp.get("away", {}) or {}).get("fullName")
        if not (hp or ap):
            return "⚾ Lanceurs probables: indisponible"
        lines = ["⚾ Lanceurs probables (si dispo):"]
        if ap: lines.append(f"- Extérieur: {ap}")
        if hp: lines.append(f"- Domicile: {hp}")
        return "\n".join(lines)
    except Exception:
        return "⚾ Lanceurs probables: indisponible"

# =========================
# UI — Menus
# =========================
def kb_sports() -> InlineKeyboardMarkup:
    rows = []
    items = list(SPORT_LABELS.items())
    for i in range(0, len(items), 2):
        row = []
        for k, label in items[i:i+2]:
            row.append(InlineKeyboardButton(label, callback_data=f"sport|{k}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Fermer", callback_data="close|x")])
    return InlineKeyboardMarkup(rows)

def kb_dates() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Aujourd’hui", callback_data="date|today"),
            InlineKeyboardButton("📅 Demain", callback_data="date|tomorrow"),
        ],
        [InlineKeyboardButton("⬅️ Retour sports", callback_data="back|sports")],
        [InlineKeyboardButton("❌ Fermer", callback_data="close|x")],
    ])

def kb_basket_leagues() -> InlineKeyboardMarkup:
    rows = []
    for code, name in BASKET_LEAGUES:
        rows.append([InlineKeyboardButton(f"🏀 {name}", callback_data=f"bleague|{code}")])
    rows.append([InlineKeyboardButton("⬅️ Retour sports", callback_data="back|sports")])
    rows.append([InlineKeyboardButton("❌ Fermer", callback_data="close|x")])
    return InlineKeyboardMarkup(rows)

def kb_matches(matches: List[Dict[str, Any]], page: int = 0, page_size: int = 10) -> InlineKeyboardMarkup:
    start = page * page_size
    end = start + page_size
    chunk = matches[start:end]

    rows: List[List[InlineKeyboardButton]] = []
    for m in chunk:
        comp = m.get("league_name") or m.get("league") or ""
        label = f"{fmt_dt(m.get('start_time',''))} — {m.get('away')} @ {m.get('home')}"
        rows.append([InlineKeyboardButton(label, callback_data=f"match|{m['id']}")])
        # petite ligne de contexte via bouton “info” (facultatif)
        rows.append([InlineKeyboardButton(f"🏆 {comp}", callback_data="noop|x")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Précédent", callback_data=f"page|{page-1}"))
    if end < len(matches):
        nav.append(InlineKeyboardButton("➡️ Suivant", callback_data=f"page|{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="back|dates")])
    rows.append([InlineKeyboardButton("🏠 Menu sports", callback_data="back|sports")])
    rows.append([InlineKeyboardButton("❌ Fermer", callback_data="close|x")])
    return InlineKeyboardMarkup(rows)

# =========================
# Telegram handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ Bot menu gratuit prêt.\nTape /menu")

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("Choisis un sport 👇", reply_markup=kb_sports())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    parts = (q.data or "").split("|")
    action = parts[0]

    if action == "noop":
        return

    if action == "close":
        await q.edit_message_text("Menu fermé ✅")
        return

    if action == "back":
        dest = parts[1]
        if dest == "sports":
            context.user_data.clear()
            await q.edit_message_text("Choisis un sport 👇", reply_markup=kb_sports())
            return
        if dest == "dates":
            await q.edit_message_text("Choisis aujourd’hui ou demain 👇", reply_markup=kb_dates())
            return

    if action == "sport":
        sport = parts[1]
        context.user_data.clear()
        context.user_data["sport"] = sport
        context.user_data["page"] = 0

        # Basket: on choisit NBA / EuroLeague
        if sport == "basketball":
            await q.edit_message_text("Choisis la compétition basket 👇", reply_markup=kb_basket_leagues())
            return

        await q.edit_message_text("Choisis aujourd’hui ou demain 👇", reply_markup=kb_dates())
        return

    if action == "bleague":
        code = parts[1]
        name = next((n for c, n in BASKET_LEAGUES if c == code), code)
        context.user_data["basket_league"] = (code, name)
        await q.edit_message_text(f"Basket: {name}\n\nChoisis aujourd’hui ou demain 👇", reply_markup=kb_dates())
        return

    if action == "date":
        sport = context.user_data.get("sport")
        if not sport:
            await q.edit_message_text("Choisis un sport 👇", reply_markup=kb_sports())
            return

        choice = parts[1]
        dt = datetime.now(timezone.utc) + (timedelta(days=1) if choice == "tomorrow" else timedelta(days=0))
        context.user_data["date_choice"] = choice
        context.user_data["page"] = 0

        await q.edit_message_text("🔎 Je récupère les matchs…")

        matches: List[Dict[str, Any]] = []
        if sport == "soccer":
            matches = await fetch_soccer_matches(dt)
        elif sport == "basketball":
            code, name = context.user_data.get("basket_league", ("nba", "NBA"))
            matches = await fetch_basket_matches(dt, code, name)
        elif sport == "tennis":
            matches = await fetch_tennis_matches(dt)
        elif sport == "nhl":
            matches = await fetch_nhl_matches_espn(dt)
        elif sport == "nfl":
            matches = await fetch_nfl_matches(dt)
        elif sport == "mlb":
            matches = await fetch_mlb_matches_espn(dt)

        matches.sort(key=lambda x: x.get("start_time", ""))

        context.user_data["matches"] = matches

        if not matches:
            await q.edit_message_text(
                "Aucun match trouvé.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Menu sports", callback_data="back|sports")]
                ])
            )
            return

        title = f"Matchs {SPORT_LABELS.get(sport, sport)} — {'Demain' if choice=='tomorrow' else 'Aujourd’hui'}"
        await q.edit_message_text(title, reply_markup=kb_matches(matches, page=0))
        return

    if action == "page":
        page = int(parts[1])
        sport = context.user_data.get("sport")
        matches = context.user_data.get("matches", []) or []
        context.user_data["page"] = page
        title = f"Matchs {SPORT_LABELS.get(sport, sport)} — {'Demain' if context.user_data.get('date_choice')=='tomorrow' else 'Aujourd’hui'}"
        await q.edit_message_text(title, reply_markup=kb_matches(matches, page=page))
        return

    if action == "match":
        sport = context.user_data.get("sport")
        match_id = parts[1]
        matches: List[Dict[str, Any]] = context.user_data.get("matches", []) or []
        picked = next((m for m in matches if m.get("id") == match_id), None)

        if not sport or not picked:
            await q.edit_message_text("Erreur: match introuvable dans la liste. Reviens au menu.", reply_markup=kb_sports())
            return

        await q.edit_message_text("📄 Je prépare la fiche…")

        # Forme (5 derniers) via ESPN schedule
        form_home = "📈 Forme: indisponible"
        form_away = "📈 Forme: indisponible"

        # mapping sport -> ESPN sport_path/league
        if sport == "soccer":
            league = picked.get("league", "eng.1")
            form_home = await team_form_espn("soccer", league, picked.get("home_id", ""))
            form_away = await team_form_espn("soccer", league, picked.get("away_id", ""))
        elif sport == "basketball":
            league = picked.get("league", "nba")
            form_home = await team_form_espn("basketball", league, picked.get("home_id", ""))
            form_away = await team_form_espn("basketball", league, picked.get("away_id", ""))
        elif sport == "nfl":
            form_home = await team_form_espn("football", "nfl", picked.get("home_id", ""))
            form_away = await team_form_espn("football", "nfl", picked.get("away_id", ""))
        elif sport == "nhl":
            form_home = await team_form_espn("hockey", "nhl", picked.get("home_id", ""))
            form_away = await team_form_espn("hockey", "nhl", picked.get("away_id", ""))
        elif sport == "mlb":
            form_home = await team_form_espn("baseball", "mlb", picked.get("home_id", ""))
            form_away = await team_form_espn("baseball", "mlb", picked.get("away_id", ""))
        elif sport == "tennis":
            # Tennis: on n’a pas toujours un team_id exploitable -> on laisse indisponible
            pass

        extra_lines: List[str] = []
        if sport == "nhl":
            extra_lines.append(await nhl_goalies(match_id))
        if sport == "mlb":
            # ESPN event id != StatsAPI gamePk. On essaie quand même:
            # Astuce: tu peux ignorer si pas dispo.
            extra_lines.append("⚠️ Pitchers MLB: dispo si ID correspond à StatsAPI (sinon ignore).")
            extra_lines.append(await mlb_pitchers(match_id))

        text: List[str] = []
        text.append(f"✅ {SPORT_LABELS.get(sport, sport)}")
        text.append(f"{fmt_dt(picked.get('start_time',''))} — {picked.get('away')} @ {picked.get('home')}")
        if picked.get("league_name"):
            text.append(f"🏆 {picked.get('league_name')}")
        text.append("")
        text.append("— Forme —")
        text.append(f"🏠 {picked.get('home')}\n{form_home}")
        text.append("")
        text.append(f"🚌 {picked.get('away')}\n{form_away}")

        if extra_lines:
            text.append("")
            text.extend(extra_lines)

        text.append("")
        text.append("🎯 Possibilités de paris :")
        for m in markets_for(sport):
            text.append(f"- {m}")

        text.append("")
        text.append("⚠️ Info & analyse = aide à la décision, pas une garantie de gains.")

        await q.edit_message_text(
            "\n".join(text),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Retour liste", callback_data=f"page|{context.user_data.get('page', 0)}")],
                [InlineKeyboardButton("🏠 Menu sports", callback_data="back|sports")],
            ])
        )
        return

    await q.edit_message_text("Commande inconnue. Tape /menu")

def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
