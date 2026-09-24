# Memo Studio: long-recording beta

This is a separate, single-user beta for the existing [Memo app](https://jayeason2680.github.io/memo/). It does not change the GitHub Pages site. The phone uploads a recording in 4 MB pieces; the service keeps processing after Safari closes. Return to the same service URL to view progress, review speaker names, and download:

1. Full timestamped transcript (`.txt`)
2. Comprehensive English and Chinese report (`.docx`)
3. Brief 3–4 paragraph summary in each language (`.docx`)

A ZIP containing all three is also available.

## Before deployment

Use a private HTTPS host with a persistent disk mounted at `/data`. Run **one service instance / one Uvicorn worker**: the included job queue and file locks are process-local. A host that sleeps or discards disk cannot provide reliable background processing. Set these service-only environment variables:

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI calls; never entered on the phone |
| `MEMO_PASSWORD` | 12+ character password to unlock the beta |
| `MEMO_SESSION_SECRET` | Random 32+ character signing secret |
| `MEMO_DATA_DIR` | Persistent data directory; defaults to `.data` locally |
| `MEMO_REPORT_MODEL` | Optional report model; defaults to `gpt-6-sol` |

Deploy the included Dockerfile and map persistent storage to `/data`. Configure HTTPS at the host or reverse proxy. Restrict host and backup access because recordings and reports are stored unencrypted on the volume. Never commit secrets or recordings. The beta has a delete action for completed, failed, or interrupted jobs; no automatic retention policy is assumed. Set a retention and backup policy before putting sensitive client material on this service.

## Audio and speaker handling

- Supports M4A, MP3, MP4, MPEG/MPGA, WAV, and WebM up to 1 GB and 24 hours. FFmpeg prepares consecutive AAC parts near 10 minutes, choosing a nearby quiet interval when available. Each part stays below the OpenAI 25 MB file limit. Ten minutes is a conservative engineering choice, not a stated OpenAI time limit.
- Personal mode uses `gpt-transcribe` with English, Mandarin, and Cantonese hints. Its timestamp marks each part, not each sentence.
- Meeting mode uses `gpt-4o-transcribe-diarize` for segment timestamps and speaker labels. Up to four optional 2–10 second voice references can link known speakers across parts. Without references, labels remain part-specific until you review and rename them. A typed name alone is never treated as proof of identity.
- Reports are generated from the entire transcript, using section notes for recordings too long for one report request. Confirm critical names, numbers, commitments, and speaker assignments against the audio.
- `gpt-4o-transcribe-diarize` has a published shutdown date of 26 February 2027. Replace that adapter before the date; keep the rest of the upload/export workflow.

## Local development

Install Python 3.12 dependencies from `requirements.txt`, install FFmpeg and FFprobe, set the three required environment variables, then run `uvicorn app:app --host 127.0.0.1 --port 8000` from this folder. A secure session cookie requires HTTPS; for local phone testing, use an HTTPS development proxy. API billing starts only when a finished job calls OpenAI.

## Current acceptance boundary

The code can be reviewed and tested locally without an API key. End-to-end transcription, report quality, iPhone Safari upload behavior, and background survival on a chosen host require a configured service and a real recording. Do not treat a code check or simulated API response as that acceptance test.

Official API references: [file transcription](https://developers.openai.com/api/docs/guides/speech-to-text), [pricing](https://developers.openai.com/api/docs/pricing), [model changes](https://developers.openai.com/api/docs/changelog).
