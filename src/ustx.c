#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "melisma.h"

static int
ml_write_yaml_string(FILE *output, const char *data, size_t length)
{
	size_t i;
	unsigned char ch;

	if (fputc('"', output) == EOF)
		return -1;

	for (i = 0; i < length; ++i) {
		ch = (unsigned char)data[i];

		switch (ch) {
		case '"':
			if (fputs("\\\"", output) == EOF)
				return -1;
			break;
		case '\\':
			if (fputs("\\\\", output) == EOF)
				return -1;
			break;
		case '\b':
			if (fputs("\\b", output) == EOF)
				return -1;
			break;
		case '\f':
			if (fputs("\\f", output) == EOF)
				return -1;
			break;
		case '\n':
			if (fputs("\\n", output) == EOF)
				return -1;
			break;
		case '\r':
			if (fputs("\\r", output) == EOF)
				return -1;
			break;
		case '\t':
			if (fputs("\\t", output) == EOF)
				return -1;
			break;
		default:
			if (ch < 0x20) {
				if (fprintf(output, "\\u%04x", ch) < 0)
					return -1;
			} else if (fputc(ch, output) == EOF) {
				return -1;
			}
			break;
		}
	}

	return fputc('"', output) == EOF ? -1 : 0;
}

static int
ml_validate_ustx(const struct ml_note_sequence *notes,
		 const struct ml_binding_sequence *bindings,
		 const struct ml_ustx_config *config,
		 uint64_t *part_duration)
{
	const struct ml_binding *binding;
	const struct ml_note *note;
	uint64_t end;
	size_t i;

	if (!config->singer || !config->phonemizer || !config->renderer ||
	    !notes->ticks_per_quarter ||
	    notes->ticks_per_quarter > INT_MAX ||
	    notes->count != bindings->count)
		return -1;

	if (notes->count && (!notes->notes || !bindings->bindings))
		return -1;

	*part_duration = 0;
	for (i = 0; i < notes->count; ++i) {
		note = &notes->notes[i];
		binding = &bindings->bindings[i];

		if (binding->note != note ||
		    note->pitch > 127 ||
		    !note->duration ||
		    note->onset > INT_MAX ||
		    note->duration > INT_MAX ||
		    note->duration > (uint64_t)INT_MAX - note->onset)
			return -1;

		switch (binding->type) {
		case ML_BINDING_ONSET:
			if (!binding->syllable ||
			    (binding->syllable->text.length &&
			     !binding->syllable->text.data))
				return -1;
			break;
		case ML_BINDING_CONTINUATION:
			if (!binding->syllable)
				return -1;
			break;
		case ML_BINDING_UNTEXTED:
			if (binding->syllable)
				return -1;
			break;
		default:
			return -1;
		}

		end = note->onset + note->duration;
		if (end > *part_duration)
			*part_duration = end;
	}

	return 0;
}

int
ml_write_ustx(FILE *output,
	      const struct ml_note_sequence *notes,
	      const struct ml_binding_sequence *bindings,
	      const struct ml_ustx_config *config)
{
	const struct ml_binding *binding;
	const struct ml_note *note;
	uint64_t part_duration;
	size_t i;

	if (!output || !notes || !bindings || !config)
		return -1;

	if (ml_validate_ustx(notes, bindings, config, &part_duration) < 0)
		return -1;

	if (fprintf(output,
		    "name: Melisma\n"
		    "comment: \"\"\n"
		    "ustx_version: \"0.7\"\n"
		    "resolution: %" PRIu32 "\n"
		    "bpm: 120\n"
		    "beat_per_bar: 4\n"
		    "beat_unit: 4\n"
		    "time_signatures:\n"
		    "- bar_position: 0\n"
		    "  beat_per_bar: 4\n"
		    "  beat_unit: 4\n"
		    "tempos:\n"
		    "- position: 0\n"
		    "  bpm: 120\n"
		    "tracks:\n"
		    "- singer: ",
		    notes->ticks_per_quarter) < 0)
		return -1;

	if (ml_write_yaml_string(output, config->singer,
				 strlen(config->singer)) < 0 ||
	    fputs("\n  phonemizer: ", output) == EOF ||
	    ml_write_yaml_string(output, config->phonemizer,
				 strlen(config->phonemizer)) < 0 ||
	    fputs("\n"
		  "  renderer_settings:\n"
		  "    renderer: ",
		  output) == EOF ||
	    ml_write_yaml_string(output, config->renderer,
				 strlen(config->renderer)) < 0 ||
	    fprintf(output,
		    "\n"
		    "  track_name: Track1\n"
		    "voice_parts:\n"
		    "- duration: %" PRIu64 "\n"
		    "  name: New Part\n"
		    "  comment: \"\"\n"
		    "  track_no: 0\n"
		    "  position: 0\n"
		    "  notes:\n",
		    part_duration) < 0)
		return -1;

	for (i = 0; i < bindings->count; ++i) {
		binding = &bindings->bindings[i];
		note = binding->note;

		if (binding->type == ML_BINDING_UNTEXTED)
			continue;

		if (fprintf(output,
			    "  - position: %" PRIu64 "\n"
			    "    duration: %" PRIu64 "\n"
			    "    tone: %u\n"
			    "    lyric: ",
			    note->onset,
			    note->duration,
			    note->pitch) < 0)
			return -1;

		switch (binding->type) {
		case ML_BINDING_ONSET:
			if (ml_write_yaml_string(output,
						 binding->syllable->text.data,
						 binding->syllable->text.length) < 0)
				return -1;
			break;
		case ML_BINDING_CONTINUATION:
			if (fputs("\"+\"", output) == EOF)
				return -1;
			break;
		case ML_BINDING_UNTEXTED:
			return -1;
		default:
			return -1;
		}

		if (fputs("\n"
			  "    pitch:\n"
			  "      data:\n"
			  "      - {x: -40, y: 0, shape: io}\n"
			  "      - {x: 40, y: 0, shape: io}\n"
			  "      snap_first: true\n"
			  "    vibrato: {length: 0, period: 175, depth: 25, in: 10, out: 10, shift: 0, drift: 0, vol_link: 0}\n"
			  "    phoneme_expressions: []\n"
			  "    phoneme_overrides: []\n",
			  output) == EOF)
			return -1;
	}

	if (fputs("  curves: []\nwave_parts: []\n", output) == EOF ||
	    ferror(output))
		return -1;

	return 0;
}
