# Matrix Cricket Bot

A Telegram bot with two things in one:

* **Hand cricket** - Solo and Team matches in groups (batter/bowler number game, captains, overs,
  timers, scorecards, player cards, orange/purple caps, rewards).
* **Auction mode** - run a live player auction in a group (teams + captains + vice-captains, sets,
  purse, timed bidding, squad PDFs, saved squads).

## Quick start

```bash
git clone <your-repo-url>
cd matrix-cricket-bot
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then edit it: BOT_TOKEN and OWNER_IDS
python bot.py
```

Needs Python 3.10+. In BotFather run `/setprivacy` -> **Disable** so the bot can read group messages.
Command list for BotFather: see `BOTFATHER_COMMANDS.txt`.

## Configuration (`.env`)

| Variable | Meaning |
| --- | --- |
| `BOT_TOKEN` | Token from @BotFather (**required**, never commit it) |
| `OWNER_IDS` | Comma separated Telegram user ids of the owners |
| `DATA_DIR` | Folder for saved data (use a persistent volume on your host) |
| `BACKUP_CHAT_ID` | Optional chat that gets a `media.json` copy after every `/addfile` |

## Access

* **Owners** (`OWNER_IDS`) can use everything.
* **Sudo users** get the owner commands (`/addfile`, `/delfile`, `/files`, `/broadcast`) and can run auctions.
  Owner replies to someone's message with `/sudo` to give access; `/rmsudo @user` removes it; `/sudo` lists them.
* `/backup` and `/restore` stay owner-only.

## Hand cricket

`/start` in a group -> pick **Solo** or **Team**. Solo: `/join`, bowlers DM the bot a number 1-6, batters
send 1-6 in the group. Team: `/add`, `/changecap`, captains pick batter/bowler with `/batting` and
`/bowling` (5 minute timer); batters may play 0-6, bowlers 1-6. A batter or bowler who times out gets
their team a **-6** penalty. `/usercard` shows your profile picture + stats.

## Auction mode

1. `/start` in the group -> **Auction**. Answer: tournament name, number of teams, max players per team, min players per team.
2. `/createteam [Team_Name] [@Captain_Username]` for every team.
3. `/addvc [Team_Name] [@VC_Username]` (change later with `/changevc`).
4. Send a message with players, one per line, then **reply** to it with `/addaucset [Set_Number] [Set_Name]`:
   ```
   1. NAME | USERNAME | USERID
   ```
5. `/setpurse [amount]`, then `/startauction`.
6. `/editauc` changes name, teams, purse, max/min players, base price, gap and timer.
7. `/end_auction` posts the squads and sends a squad PDF per team and the unsold-list PDF.
8. `/squads` shows the saved squads of every finished tournament.

Players can register in the bot's DM (`/start` -> **Player Registration**) when the host opens registration.

## Media (`/addfile`)

`/addfile <key>` - reply to a photo / GIF / video, or send it with that caption. Several files per key are
allowed (one is picked at random); `/delfile <key>` removes them, `/files` lists every key.

* Hand cricket keys: `0`-`6`, `wicket`, `duck`, `hattrick`, `fifty`, `century`, `welcome`, `groupwelcome`,
  `lobby`, `arena`, `bowling`, `facetheball`, `newbatter`, `scorecard`, `potm`, `usercard`, `teamlobby`,
  `teamarena`, `captain`.
* Auction keys: `aucwelcome`, `aucregister`, `aucsets`, `aucintro`, `auclive`, `aucsold`, `aucunsold`,
  `auctrophy`, `aucplayer` (picture for players with no profile photo).

Anything you do not set falls back to the bundled files in `media/`.

## Keeping data when you change server

* Set `DATA_DIR` to a persistent volume, **or**
* owner sends `/backup`, and on the new server replies to each file with `/restore` (then restart).

## Project layout

```
bot.py            main bot (hand cricket) + startup
auction_mode.py   auction mode, loaded by bot.py
cards.py          lot-card images (Pillow)
reports.py        squad / unsold PDFs (reportlab)
media/            bundled banners, GIFs, font
```

## Security

Never commit `.env`, `media.json`, `stats.db` or `auction_*.json` (they are in `.gitignore`).
If a bot token was ever committed or shared, revoke it in BotFather (`/revoke`) and use the new one.
