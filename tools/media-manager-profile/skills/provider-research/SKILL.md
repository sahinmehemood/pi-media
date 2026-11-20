---
name: provider-research
description: Identify titles with evidence — IDs first, two sources agree
version: 3.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [research, tmdb, tvdb, anilist, kitsu, identification]
    category: media
    requires_toolsets: [terminal]
---

# Provider research — how to identify with evidence

## When to Use

For every title you must identify or number, before writing any
caption. The app serves all provider data through ONE gateway —
you never call providers raw:

- `librarian_cli.py lookup <name> [--type <type>]` — merged
  identity: every known ID + every title variant per candidate.
- `librarian_cli.py series <provider:id>` — episode lists:
  aired / absolute / Season 0 specials, titles + air dates.
- `librarian_cli.py crossid <provider:id>` — every other ID.

Results are cached; a provider that is down is named in
`providers_down`. Down is weather, never "no match".

## Who owns what (research-settled)

| Media | Identity & titles | Numbering |
|---|---|---|
| Western series | TMDB | TVDB aired |
| Movies | TMDB (IMDb = join key) | — |
| Cartoons | TVDB | TVDB aired/absolute |
| Anime series | AniList | TVDB aired + absolute |
| Long-running anime | AniList | TVDB absolute only |
| Anime movie/OVA | AniList/MAL | the entry is the unit |
| Donghua | AniList + TMDB jointly | per show — see below |

- **AniList** is the anime/donghua title bridge: romaji (≈pinyin),
  English, native, synonyms, one entry per cour, `idMal` hop. It
  has NO episode lists.
- **TVDB** is the numbering backbone: aired + absolute + Season 0
  on one record set, `remoteIds` for cross-ID. Its search rows
  and records can still be the WRONG namesake — verify by year
  and episode titles.
- **TMDB** carries donghua jointly with AniList; its season
  splits differ from TVDB's, and its Season 0 often holds the
  special arcs TVDB lacks. When both answer, `series` returns
  both lists — compare them; that comparison is the proof.
- **Jikan/MAL** episode lists are unreachable from this server
  (listed in `providers_down`); do not wait for them.
- **Kitsu** is a mappings oracle/fallback only. **IMDb** has no
  API — it is a join key (via Cinemeta).

## The verification standard

1. Identity is proven by IDs, never by name similarity. Two
   names for one show (pinyin vs western) join only through a
   shared ID.
2. Numbering is proven by EPISODE TITLE or AIR DATE against the
   actual file — never by episode counts.
3. Absolute ↔ season/episode conversions are LOOKED UP in the
   provider's own absolute list, never computed by summing
   season lengths.
4. Donghua (and any split-cour show): decide ONCE which
   provider's numbering the files follow, prove it by titles,
   and store it in the series record (`record-set`). Follow the
   record forever after.
5. No proof → the series is conflicted. Escalate with the
   evidence; never pick the least-bad numbering silently.
