#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>

#include "melisma.h"

#define ML_ABC_UNITS_PER_QUARTER 8
#define ML_ABC_UNITS_PER_BAR 32

struct ml_abc_writer {
	FILE *output;
	uint64_t position;
	int has_token;
};

static int
ml_ticks_to_abc_units(uint64_t ticks,
		      uint32_t ticks_per_quarter,
		      uint64_t *units)
{
	uint64_t quotient;
	uint64_t remainder;
	uint64_t remainder_units;

	quotient = ticks / ticks_per_quarter;
	remainder = ticks % ticks_per_quarter;
	remainder_units = remainder * ML_ABC_UNITS_PER_QUARTER;

	if (remainder_units % ticks_per_quarter)
		return -1;

	if (quotient > UINT64_MAX / ML_ABC_UNITS_PER_QUARTER)
		return -1;

	*units = quotient * ML_ABC_UNITS_PER_QUARTER;
	remainder_units /= ticks_per_quarter;
	if (UINT64_MAX - *units < remainder_units)
		return -1;

	*units += remainder_units;
	return 0;
}

static int
ml_validate_abc(const struct ml_note_sequence *notes, uint64_t *total_units)
{
	const struct ml_note *note;
	uint64_t onset_units;
	uint64_t duration_units;
	uint64_t previous_onset = 0;
	uint64_t previous_end = 0;
	uint64_t end;
	size_t i;

	if (!notes->ticks_per_quarter)
		return -1;

	if (notes->count && !notes->notes)
		return -1;

	for (i = 0; i < notes->count; ++i) {
		note = &notes->notes[i];

		if (note->pitch > 127 || !note->duration)
			return -1;

		if (i && note->onset < previous_onset)
			return -1;

		if (UINT64_MAX - note->onset < note->duration)
			return -1;
		end = note->onset + note->duration;

		if (i && note->onset < previous_end)
			return -1;

		if (ml_ticks_to_abc_units(note->onset,
					  notes->ticks_per_quarter,
					  &onset_units) < 0 ||
		    ml_ticks_to_abc_units(note->duration,
					  notes->ticks_per_quarter,
					  &duration_units) < 0 ||
		    UINT64_MAX - onset_units < duration_units)
			return -1;

		previous_onset = note->onset;
		previous_end = end;
		*total_units = onset_units + duration_units;
	}

	return 0;
}

static int
ml_abc_pitch(unsigned int pitch, char *text, size_t size)
{
	static const char *const names[] = {
		"=C", "^C", "=D", "^D", "=E", "=F",
		"^F", "=G", "^G", "=A", "^A", "=B",
	};
	const char *name;
	unsigned int octave;
	size_t offset = 0;
	int delta;
	int written;

	if (pitch > 127)
		return -1;

	name = names[pitch % 12];
	octave = pitch / 12;
	delta = (int)octave - 5;

	if (name[0] == '^' || name[0] == '=') {
		if (offset == size)
			return -1;
		text[offset++] = name[0];
		++name;
	}

	if (offset == size)
		return -1;
	text[offset++] = delta > 0 ? (char)(name[0] + ('a' - 'A')) : name[0];

	if (delta > 1) {
		written = delta - 1;
		while (written--) {
			if (offset == size)
				return -1;
			text[offset++] = '\'';
		}
	} else if (delta < 0) {
		written = -delta;
		while (written--) {
			if (offset == size)
				return -1;
			text[offset++] = ',';
		}
	}

	if (offset == size)
		return -1;
	text[offset] = '\0';
	return 0;
}

static int
ml_abc_write_segment(struct ml_abc_writer *writer,
		     const char *symbol,
		     uint64_t length,
		     int tie)
{
	if (writer->position &&
	    writer->position % ML_ABC_UNITS_PER_BAR == 0) {
		if (fputs(" |", writer->output) == EOF)
			return -1;
	}

	if (writer->has_token && fputc(' ', writer->output) == EOF)
		return -1;

	if (fprintf(writer->output, "%s%" PRIu64 "%s",
		    symbol, length, tie ? "-" : "") < 0)
		return -1;

	writer->position += length;
	writer->has_token = 1;
	return 0;
}

static int
ml_abc_write_event(struct ml_abc_writer *writer,
		   const char *symbol,
		   uint64_t length,
		   int tie)
{
	uint64_t bar_remaining;
	uint64_t segment;

	while (length) {
		bar_remaining = ML_ABC_UNITS_PER_BAR -
				writer->position % ML_ABC_UNITS_PER_BAR;
		segment = length < bar_remaining ? length : bar_remaining;
		length -= segment;

		if (ml_abc_write_segment(writer, symbol, segment,
					 tie && length) < 0)
			return -1;
	}

	return 0;
}

static int
ml_abc_finish_voice(struct ml_abc_writer *writer)
{
	if (writer->position &&
	    writer->position % ML_ABC_UNITS_PER_BAR == 0 &&
	    fputs(" |", writer->output) == EOF)
		return -1;

	return 0;
}

static int
ml_abc_write_vocal(FILE *output, const struct ml_note_sequence *notes)
{
	struct ml_abc_writer writer = {
		.output = output,
	};
	const struct ml_note *note;
	char pitch[16];
	uint64_t onset;
	uint64_t duration;
	size_t i;

	for (i = 0; i < notes->count; ++i) {
		note = &notes->notes[i];

		if (ml_ticks_to_abc_units(note->onset,
					  notes->ticks_per_quarter,
					  &onset) < 0 ||
		    ml_ticks_to_abc_units(note->duration,
					  notes->ticks_per_quarter,
					  &duration) < 0 ||
		    ml_abc_pitch(note->pitch, pitch, sizeof(pitch)) < 0)
			return -1;

		if (writer.position < onset &&
		    ml_abc_write_event(&writer, "z",
				       onset - writer.position, 0) < 0)
			return -1;

		if (ml_abc_write_event(&writer, pitch, duration, 1) < 0)
			return -1;
	}

	return ml_abc_finish_voice(&writer);
}

static int
ml_abc_write_instrument(FILE *output, uint64_t total_units)
{
	struct ml_abc_writer writer = {
		.output = output,
	};

	if (total_units &&
	    ml_abc_write_event(&writer, "z", total_units, 0) < 0)
		return -1;

	return ml_abc_finish_voice(&writer);
}

int
ml_write_abc(FILE *output, const struct ml_note_sequence *notes)
{
	uint64_t total_units = 0;

	if (!output || !notes)
		return -1;

	if (ml_validate_abc(notes, &total_units) < 0)
		return -1;

	if (fputs("X:1\n"
		  "T:\n"
		  "M:4/4\n"
		  "L:1/32\n"
		  "Q:1/4=120\n"
		  "V: Vocal clef=treble name=\"Vocal Melody\" snm=\"Vocal\"\n"
		  "V: Ins clef=treble name=\"Ins Melody\" snm=\"Inst.\"\n"
		  "K:C\n"
		  "\n"
		  "V: Vocal\n",
		  output) == EOF ||
	    ml_abc_write_vocal(output, notes) < 0 ||
	    fputs("\n\nV: Ins\n", output) == EOF ||
	    ml_abc_write_instrument(output, total_units) < 0 ||
	    fputc('\n', output) == EOF ||
	    ferror(output))
		return -1;

	/* The caller owns the stream and is responsible for flushing it. */
	return 0;
}
