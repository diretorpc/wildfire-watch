# 🔥 Wildfire Watch

**A satellite wildfire watchdog for family farms — built to run for free, and built to admit when it is blind.**

Every 10 minutes it pulls satellite hotspot data for all of Brazil, checks whether
any fire landed inside (or near) a registered farm, and emails whoever can actually
do something about it.

Portuguese version: **[README-pt.md](README-pt.md)** · Built for real use on family
farms in Minas Gerais, Brazil. This repository ships example farm data; the real
boundaries live in a private repository.

![Monitoring panel](docs/panel.png)

---

## The problem

A wildfire in dry pasture moves at 3–4 km/h. It usually starts on someone else's
land — a roadside, a neighbour's burn that got away — and arrives at your fence
before anyone at the house sees smoke. At night nobody sees it at all.

Commercial fire-detection platforms exist and charge accordingly. All the data
they resell is public: Brazil's INPE (National Institute for Space Research)
publishes satellite hotspots for the entire country every 10 minutes, for free.

So the interesting problem is not detection. It's everything around it.

## What it actually does

- **Watches** — downloads INPE's 10-minute hotspot feed, tests every hotspot
  against each farm's boundary plus two rings (5 km = urgent, 10 km = approaching).
- **Groups** — hotspots within 2 km of each other are one fire, not five emails.
- **Escalates** — a fire that gets closer breaks through the 60-minute quiet
  period. Fire moving toward you never gets silenced.
- **Alerts** — email with distance, bearing, map link, **who to call**, **where the
  water is**, and a fixed safety warning.
- **Reports** — a daily "I'm alive" summary, and a loud alarm when it goes blind.
- **Shows** — a local browser panel over real satellite imagery.

## The design decision I'd defend in an interview

**A monitoring system that can fail silently is worse than no monitoring system.**

The first version had a bug that no test caught and no user would notice. When
INPE's server went down, the watcher logged a warning — to a console window that
does not exist, because it runs headless — and carried on. At 6 pm the daily
summary still said:

> ✅ Watcher up. Watched your 5 farms. No fire alerts — **all clear**.

That sentence could be a lie. The counter behind it was zero both when nothing
burned and when nothing was *looked at*. On a fire-season night, that is the most
dangerous possible output: false comfort.

The fix reframes the whole system around one distinction — **"running" is not
"seeing"**:

- every cycle returns `ok` or `blind`, including the treacherous case where the
  server answers but stopped publishing (stale data guard, 60-minute threshold —
  measured real-world delay is ~6 minutes, so 10x headroom);
- a cycle that crashes counts as blind too, not as "fine";
- six blind cycles in a row (~1 hour) trigger an emergency email stating plainly
  that the farms are **not being watched right now**;
- the daily summary can no longer say "all clear" without having looked. It has
  three shapes — clean / partially blind / blind all day — and **the subject line
  changes with them**, because the subject is what you read on a phone.

## Other decisions worth naming

| Decision | Choice | Why |
|---|---|---|
| Detection method | Plain geometry, **no ML** | A point-in-rectangle test solves it. ML would add cost, latency and unpredictability to a system whose only real currency is trust. |
| Alert volume | Hard target: **1–2 emails/day** in fire season | ~1,000 km² are watched against ~27 km² of farmland. Flood the inbox in August and the owner stops reading — the system then fails completely while appearing to work. |
| Third-party addresses | Environment variables | INPE can move its endpoint whenever it likes. Anything a third party controls does not belong in source code. |
| Missing config | **Complained about, never omitted** | An alert with no phone number prints "no phone registered — fill it in". A silent gap is worse than a visible one. |
| Dependencies | **Zero** (Python standard library only) | Nothing to install, nothing to break on upgrade, nothing to pay for. Leaflet is vendored locally so the panel works offline. |
| Urgent alerts | Ship a safety warning | An email that says "run" can send someone alone, on a motorbike, into a wind-driven fire front. Whoever tells you to run owes you the instructions for not dying. |
| Land that isn't yours | **Watch-only zones** | Set `apenas_observacao` on an area and it appears on the panel and in the daily summary but **never emails**. Alerting on someone else's land — where there is no tractor to send and no fence to defend — produces no action, it only spends the one irreplaceable resource in the system: the owner's attention. |

## How it works

```
INPE 10-min CSV  ──►  parse  ──►  geometric test      ──►  group ≤2 km  ──►  cooldown +
(all of Brazil)                   (boundary + rings)       (one fire =        escalation
                                                            one alert)             │
                                                                                   ▼
   panel-state.json  ◄── panel (Leaflet + Esri imagery)              email (SMTP)
   watcher-state.json ◄── health counters, per-farm cooldown
```

Everything lives in one process with a 10-minute loop, an OS-level lock so two
copies can't fight, and atomic state writes so a power cut can't leave half a file.

## Running it

No `pip install` — Python 3.10+ and nothing else.

```bash
cp .env.example .env          # fill in SMTP credentials
python -X utf8 vigia.py --teste-email   # verify email delivery
python -X utf8 vigia.py --uma-vez       # one cycle, then exit
python -X utf8 vigia.py                 # watch continuously
python painel.py                        # browser panel on 127.0.0.1:8000
```

Register a farm from an official CAR registry number (Brazil's Rural Environmental
Registry), or from a KMZ/KML file exported from Google Earth:

```bash
python -X utf8 tools/cadastrar_fazenda.py --car "UF-9999999-XXXX...." --nome "North Field"
python -X utf8 tools/cadastrar_fazenda.py --kmz boundary.kmz --nome "North Field"
```

The KMZ path refuses to register a farm more than 150 km from the others without
an explicit override — that is the check that catches swapped latitude/longitude,
an error that produces no error message, just a watchdog guarding a stranger's land.

## Fire drill — testing the whole chain against a real fire

Knowing the watcher is running is **not** the same as knowing the alert reaches
someone who can act. The drill proves the entire chain — download, detect, group,
compose, send, arrive — using a wildfire that is actually burning somewhere in
Brazil right now.

```bash
python -X utf8 tools/simulado.py                  # find an active fire, send the alert
python -X utf8 tools/simulado.py --sem-email      # show what would be sent, send nothing
python -X utf8 tools/simulado.py --perto-de -16.75,-47.93 --raio-km 100
```

The email arrives tagged **🧪 SIMULADO** in the subject and first line, so nobody
acts on it thinking their own land is burning.

**It touches nothing.** No config change, no state change (pointer, cooldown, daily
counter), no lock contention with the running watcher. Nothing to clean up
afterwards — deliberately so. The obvious alternative, registering a temporary
"test farm", works just as well and adds a risk that isn't worth it: forget to
remove it, and by August a daily alert has trained the owner to ignore the inbox.

When `--perto-de` finds nothing, the script says plainly that this is **absence of
fire, not failure of the watcher**. A test that can fail for two different reasons
proves nothing.

⚠️ The email is the easy half. **Time the other half:** from alert received to
someone on site with equipment. If the answer is "not sure", the system isn't
ready — however good the code is.

## Tests

```bash
python -m unittest discover -s tests -v    # 117 tests
```

Pure functions are tested directly; the cycle is tested with injected fakes for
network, email and disk. The tests that matter most are the ones asserting the
system **refuses to claim it is fine** — for example
`test_sem_nenhum_ciclo_bom_nunca_diz_que_esta_tudo_certo` ("with no good cycle it
never says everything is fine").

## Honest limitations

- **The first 30–60 minutes are invisible.** The GOES-19 satellite has a ~2 km
  pixel: it sees fire that already has a front, not a fire that just started.
  Total time from ignition to email is 30 minutes to 1h40. This is a night watchman
  with binoculars, not a smoke detector.
- **Fire under canopy is poorly seen** — which is exactly where the legally
  protected native vegetation is.
- **Thick cloud blocks the infrared** the satellite measures.
- **It is the second layer.** Firebreaks, equipment and neighbours are the first.
  A monitoring system that makes people relax their guard has increased risk,
  not reduced it.

## License

MIT — see [LICENSE](LICENSE).

The panel interface is in Portuguese: it was built for the people who actually
work the land, not for the portfolio.
