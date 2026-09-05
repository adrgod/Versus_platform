# Steps on the Roadmap

## PHASE 1 — MUSIC LIBRARY SCANNER
- Create a Git repository for the project
- Set up a Python virtual environment
- Recursively scan the main music folder and all YYYYMMDD subfolders
- Read MP3 metadata using Mutagen
- Extract:
- Artist
- Album
- Track title
- Track number
- Year
- Genre
- Album artist
- Label (if available)
- Embedded artwork
- Store the file path and metadata
- Identify missing/incomplete tags
- Produce a report of files with missing metadata
- Never modify files automatically at first

Mutagen's EasyID3 tags:

```
dict_keys(['album', 'bpm', 'compilation', 'composer', 'copyright', 'encodedby', 'lyricist', 'length', 'media', 'mood', 'grouping', 'title', 'version', 'artist', 'albumartist', 'conductor', 'arranger', 'discnumber', 'organization', 'tracknumber', 'author', 'albumartistsort', 'albumsort', 'composersort', 'artistsort', 'titlesort', 'isrc', 'discsubtitle', 'language', 'genre', 'date', 'originaldate', 'performer:*', 'musicbrainz_trackid', 'website', 'replaygain_*_gain', 'replaygain_*_peak', 'musicbrainz_artistid', 'musicbrainz_albumid', 'musicbrainz_albumartistid', 'musicbrainz_trmid', 'musicip_puid', 'musicip_fingerprint', 'musicbrainz_albumstatus', 'musicbrainz_albumtype', 'releasecountry', 'musicbrainz_discid', 'asin', 'performer', 'barcode', 'catalognumber', 'musicbrainz_releasetrackid', 'musicbrainz_releasegroupid', 'musicbrainz_workid', 'acoustid_fingerprint', 'acoustid_id'])
````

## PHASE 2 — MUSIC DATABASE
- Use SQLite as the initial database
- Import the scanned music library
- Identify albums rather than treating every track as a separate work
- Handle LPs and EPs
- Link:
- Albums → Artists
- Albums → Labels
- Albums → Genres
- Albums → Tracks
- Keep the original file path
- Record when metadata was last scanned/enriched
- Make the import process safe to run repeatedly

## PHASE 3 — METADATA CLEAN-UP
- Decide which metadata fields are considered mandatory
- Detect duplicate albums/tracks
- Identify inconsistent artist/album names
- Identify albums with missing artwork
- Identify albums with missing year/genre/label
- Generate reports before making changes
- Eventually add optional automatic tag fixing

## PHASE 4 — EXTERNAL MUSIC DATA ENRICHMENT
- Investigate MusicBrainz as a source of structured music data
- Investigate Discogs and other useful sources
- Match albums/artists despite differences in spelling
- Enrich missing information such as:
- Release date/year
- Country
- Label
- Genre/style
- Artist information
- Band members
- Instruments
- Line-up changes
- Related releases
- Keep track of where each piece of information came from
- Record confidence/matching information
- Do not overwrite existing information blindly
- Make enrichment an optional, repeatable process

## PHASE 5 — MAGAZINE PDF ARCHIVE
- Collect the previous magazine PDFs
- Process the magazines issue by issue
- Extract text from PDFs
- Use OCR for scanned/image-only pages
- Keep the original extracted text
- Identify articles within each issue
- Classify articles:
- Album review
- Interview
- News
- Other
- Store the issue date/number and page information

## PHASE 6 — EXTRACT REVIEWS
- Detect individual album reviews
- Extract:
- Artist
- Album
- Reviewer
- Rating
- Review text
- Issue
- Publication date
- Link reviews to albums in the music database
- Link reviews to artists
- Link reviews to genres
- Link reviews to labels
- Identify reviews that could not be matched automatically
- Provide a way to manually correct matches

## PHASE 7 — AI ANALYSIS OF REVIEWS
- Use an LLM to analyse review text
- Extract keywords
- Extract themes
- Identify musical characteristics
- Identify recurring descriptions
- Analyse sentiment/opinion
- Keep the original review text alongside the AI-generated data
- Make AI analysis reproducible/re-runnable
- Don't restrict the database to only predefined AI fields

## PHASE 8 — INTERVIEW ARCHIVE
- Detect interviews in the magazine PDFs
- Extract:
- Artist
- Interviewer
- Issue
- Date
- Questions
- Answers
- Separate questions from answers
- Analyse recurring questions
- Find the most frequently asked questions
- Group similar questions even when wording differs
- Identify recurring interview themes
- Experiment with other useful LLM analysis later
- Keep this exploratory rather than over-engineering it

## PHASE 9 — EDITORIAL / HISTORICAL ANALYSIS
- Find all artists reviewed by the magazine
- Find artists that were never reviewed
- Find albums that were never reviewed
- Find artists interviewed but never reviewed
- Find labels covered by the magazine
- Find which labels have the most reviews
- Find which countries/regions are represented
- Find the most prolific reviewers
- Find reviews per year
- Find albums/reviews by genre
- Analyse how genres changed over time
- Analyse which genres were reviewed most/least
- Compare releases received vs. releases actually reviewed
- Find gaps in the archive

## PHASE 10 — THE "REDISCOVERIES" IDEA
- Find albums from previous years that were never reviewed
- Search for albums that are 5/10/15+ years old and were never reviewed
- Filter by genre, label, year, etc.
- Create a shortlist of interesting forgotten albums
- Potentially use this to inspire a recurring "Album Never Reviewed" feature
- Keep this as a feature rather than making it part of the core system

## PHASE 11 — DJ / MUSIC LIBRARY WEB INTERFACE
- Start learning Django through this project
- Create a basic Django application
- Connect Django to the SQLite database
- Create an album browser
- Display:
- Artwork
- Artist
- Album
- Year
- Genre
- Label
- Track listing
- Add search
- Add filtering
- Filter by genre/year/artist/label/reviewer/review status
- Create an artist page
- Create an album page
- Show the magazine review directly on the album page
- Show interviews associated with the artist
- Show related albums

## PHASE 12 — BUILT-IN MUSIC PLAYER
- Add MP3 playback to the Django interface
- Play an album directly from its album page
- Add play/pause/seek/volume
- Add track selection
- Add a queue
- Remember the current queue
- Keep the player simple and offline-oriented
- Do not try to recreate Spotify

## PHASE 13 — VISUALISATION / DISCOVERY
- Reviews by year
- Reviews by genre
- Reviews by writer
- Artists reviewed over time
- Labels reviewed over time
- Genre evolution over the years
- Most reviewed artists
- Most reviewed labels
- Most prolific writers
- Albums with no review
- Artists with no review
- Interactive charts where useful

## PHASE 14 — SEMANTIC SEARCH / LLM EXPLORATION
- Add semantic search over reviews and interviews
- Search by meaning rather than exact words
- Example:
"Find reviews describing dark atmospheric black metal"
- Search across reviews and interviews
- Experiment with questions that weren't anticipated when designing the database
- Consider embeddings/vector search only when ordinary database search becomes insufficient

## PHASE 15 — AUTOMATION
- Make the music scanner runnable whenever new files arrive
- Automatically detect new albums
- Automatically detect missing metadata
- Run enrichment only for new/changed albums
- Process new magazine PDFs automatically
- Detect new reviews/interviews
- Queue AI analysis for new content
- Keep everything incremental rather than rescanning hundreds of GB unnecessarily

# IMPORTANT DEVELOPMENT PRINCIPLE

- Start SMALL
- Don't build the entire system before using it
- First goal: scan the music folder and create a searchable SQLite database
- Second goal: build a very simple Django album browser
- Third goal: import one or two magazine PDFs
- Fourth goal: extract reviews and link them to albums
- Then gradually add enrichment, AI, interviews, visualisations and the player

FIRST VERSION / MVP

- Python
- Mutagen
- SQLite
- Django
- Music folder scanner
- Album database
- Basic search
- Basic album page
- Artwork
- Track listing
- Review status

Everything else can wait.

The main idea:
MUSIC FILES → METADATA → SQLITE → MAGAZINE PDFs → REVIEWS/INTERVIEWS → ENRICHMENT → DJANGO → SEARCH / ANALYSIS / PLAYER