# Memo Studio beta acceptance

The draft is ready for code and design review. A hosted release is still pending.

## Verified locally

- Follow-up integrity audit: 14 offline tests pass, including a 210 MiB synthetic upload in 4 MiB requests, with SHA-256 comparison of the complete stored file. This uses the in-process HTTP test client, not an iPhone, real audio, or a hosted network.
- Resume rejects storage shorter than its acknowledged offset; extra unacknowledged bytes are safely replaced. Finish verifies the stored file length before queuing. Metadata contents and directory renames are flushed before acknowledging progress.
- 12 offline tests pass, including translation checkpoint recovery, rejecting incomplete translations, preserving original text, and bilingual exports.
- Python and JavaScript syntax checks pass.
- Local browser: sign-in, import selection, language switching, full report, original/translated comparison, and recovery UI.
- Throttled 12 MB disposable upload: paused after 4 MB; a changed file with the same name and size was rejected; the original resumed to 12 MB. The invalid audio then displayed the expected recovery screen. No OpenAI call was made.
- Reports used in the design preview are clearly labelled samples, written for layout review.

## Before approving a hosted release

Use a personal or otherwise approved recording, with the API key entered directly in the host. Hosting approval is separate from this draft PR.

1. Import a real Apple Voice Memo over 200 MB on iPhone Safari. Check pause/resume, a connection interruption, and returning after upload. Confirm the file remains complete.
2. Use a short English/Mandarin conversation with overlapping speech, names, numbers, negation and uncertain plans. Compare transcript and both translations with the audio; verify no suggestion becomes a commitment.
3. Use a long conversation to check beginning/middle/end coverage, section boundaries, speaker ambiguity, the context of decisions and action owners/dates.
4. Check the actual API account can use both transcription routes and the configured report model. Record processing time and API cost, including translations.
5. Restart the hosted service during a job and confirm it resumes from saved sections. A request interrupted after provider completion may be billed again on retry.
6. Open the Word files in Pages or Word on iPhone. Check Chinese glyphs, paragraph breaks, the action table, long transcript page breaks and Save to Files/ZIP handling.
7. Review the service's storage and backup retention before using sensitive recordings. Test deletion only with disposable sample data.

Speaker names are reviewed after generation. Saving names updates labels but does not regenerate the report. The diarization adapter requires a replacement before the published February 2027 shutdown.
