# Location targeting and outreach review

## Set up a search

In `Keywords`, add the niche, search term, and enabled flag. Add optional criteria to the same row. Duplicate the row to run the same keyword for a different area.

| Field | How it works |
| --- | --- |
| Country | Enter a country name or code, such as `Canada` or `CA`. Leave blank for any country. |
| City / Town | Enter a place such as `Toronto`. For names shared by multiple places, enter the city and region as published by the creator and include the country. |
| Location Policy | `Review uncertain` is the default. It keeps ambiguous matches in Needs Review. `Confirmed only` excludes them. |
| Min Subscribers and Max Subscribers | Blank uses 1,000 through 1,000,000 inclusive. Enter whole numbers to broaden or tighten the range. Hidden counts go to review. |
| Excluded Countries | Blank uses `BD, IN, PK` except for the country explicitly targeted by that row. Enter comma-separated names or codes to choose your own list, or enter `None` to remove the default exclusions. |
| Search Mode | Blank uses Channel search. Video search matches individual videos and traces them back to creator channels. |

Example row: `Real Estate`, `Real Estate Podcast`, `TRUE`, `CA`, `Toronto`, `Review uncertain`, `500`, `2000000`. Use a copy of the row for a second city. Leave geography blank to keep an existing search worldwide. Rows with invalid countries, contradictory exclusions, or reversed subscriber bounds are skipped and counted as input errors in the run log.

## How the agent decides

1. Python reads enabled rows and appends the requested city and country name to the YouTube query. It uses the ordinary YouTube Data API search followed by batched channel details. A place in the query or title is a candidate signal. YouTube's `regionCode` is video viewability, and its radius filter works on geo-tagged videos. ChannelScout uses neither as proof of creator location. [YouTube search parameters](https://developers.google.com/youtube/data/reference/rest/v3/search/list)
2. The requested country must match the channel's reported country. An explicitly different country is excluded. Where a city is requested, a clear current statement such as “We’re based in Toronto” in the channel description supplies city evidence. A place mentioned as a topic, a missing country for a country-targeted row, and other unclear statements are handled by the location policy. A reported country or public description is self-reported evidence. [YouTube channel metadata](https://developers.google.com/youtube/data/reference/rest/v3/channels)
3. Reported subscribers, configured country exclusions, and any enabled activity rule determine selection. The optional 90-day rule remains disabled by default. Target Format is recorded as the operator's preference for review. [Operator runbook](operations.md)
4. PostgreSQL keeps one channel record plus a separate decision for each keyword and criteria combination. A channel remains qualified when at least one enabled, current match qualifies. Deactivating the qualifying keyword recalculates the result using other active matches.
5. Public contact lookup runs only on qualifying channels. A single publicly listed business address and usable channel evidence allow a draft. The model receives the offer and source facts, writes one relevant observation and one clear ask, and can abstain. Python validates the draft. Geography, qualification, contact verification, and delivery permissions are deterministic workflow decisions.

## How to read the colors

| Row color | Priority | Rule |
| --- | --- | --- |
| Green | High | Qualified with a verified public business email and a draft awaiting review. |
| Blue | Standard | Qualified and still needs contact or draft work. |
| Amber | Review | Eligibility needs checking, for example because location or subscriber count is unavailable. |
| Light red | Excluded | An existing lead failed the active criteria or has no active qualifying search. Freshly excluded candidates appear in the run totals. |

`Target Area`, `Location Match`, and `Selection Reason` show the supporting context in the Sheet. These colors indicate ChannelScout's work readiness. Subscriber figures and channel country remain source fields.

## Review drafts in Excel

The Excel review includes the public contact source, subject, draft, target location, and editable decision and notes fields. A worksheet review is a working copy for your judgment. It cannot submit an email or update Sheets approvals. The sender address stays blank until you enter the mailbox you selected. Only use approved outreach through your chosen sending workflow.

Run `channel-scout export-outreach outreach-review.csv` to create a fresh Excel-compatible review CSV from the current qualified drafts. It exports no review tokens or credentials and refuses to overwrite an existing file. The Google Sheet keeps the draft version and approval workflow.

## Database and tool changes

Migration `003_keyword_location.sql` adds the criterion settings to `keywords` and the selection, reason, and location evidence category to `channel_matches`. It retains existing channel IDs and drafts. The existing Sheets and YouTube adapters do the extra work. No geocoder, new credentials, or LLM search tool is required. The only model task remains drafting.
