#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>

#include "melisma.h"

#define ML_ABC_UNITS_PER_QUARTER 8
#define ML_ABC_UNITS_PER_BAR 32
#define ML_ABC_MAX_GROUP_MEASURES 4

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
	if (writer->has_token &&
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
		   int tie_final)
{
	uint64_t bar_remaining;
	uint64_t segment;

	while (length) {
		bar_remaining = ML_ABC_UNITS_PER_BAR -
				writer->position % ML_ABC_UNITS_PER_BAR;
		segment = length < bar_remaining ? length : bar_remaining;
		length -= segment;

		if (ml_abc_write_segment(writer, symbol, segment,
					 length || tie_final) < 0)
			return -1;
	}

	return 0;
}

static int
ml_abc_write_full_rest(struct ml_abc_writer *writer,
		       unsigned int measures)
{
	if (!measures || measures > ML_ABC_MAX_GROUP_MEASURES)
		return -1;

	if (writer->has_token) {
		if (fputs(" | ", writer->output) == EOF)
			return -1;
	}

	if (measures == 1) {
		if (fputc('Z', writer->output) == EOF)
			return -1;
	} else if (fprintf(writer->output, "Z%u", measures) < 0) {
		return -1;
	}

	writer->position += (uint64_t)measures * ML_ABC_UNITS_PER_BAR;
	writer->has_token = 1;
	return 0;
}

static int
ml_abc_write_rest(struct ml_abc_writer *writer, uint64_t length)
{
	uint64_t bar_remaining;
	uint64_t segment;
	uint64_t measures;

	while (length) {
		if (writer->position % ML_ABC_UNITS_PER_BAR == 0 &&
		    length >= ML_ABC_UNITS_PER_BAR) {
			measures = length / ML_ABC_UNITS_PER_BAR;
			if (measures > ML_ABC_MAX_GROUP_MEASURES)
				measures = ML_ABC_MAX_GROUP_MEASURES;

			if (ml_abc_write_full_rest(writer,
						   (unsigned int)measures) < 0)
				return -1;
			length -= measures * ML_ABC_UNITS_PER_BAR;
			continue;
		}

		bar_remaining = ML_ABC_UNITS_PER_BAR -
				writer->position % ML_ABC_UNITS_PER_BAR;
		segment = length < bar_remaining ? length : bar_remaining;
		if (ml_abc_write_segment(writer, "z", segment, 0) < 0)
			return -1;
		length -= segment;
	}

	return 0;
}

static int
ml_abc_finish_voice(struct ml_abc_writer *writer)
{
	if (!writer->has_token ||
	    writer->position % ML_ABC_UNITS_PER_BAR != 0)
		return -1;

	return fputs(" |", writer->output) == EOF ? -1 : 0;
}

static int
ml_abc_write_vocal_group(FILE *output,
			 const struct ml_note_sequence *notes,
			 uint64_t group_start,
			 uint64_t group_end)
{
	struct ml_abc_writer writer = {
		.output = output,
		.position = group_start,
	};
	const struct ml_note *note;
	char pitch[16];
	uint64_t onset;
	uint64_t duration;
	uint64_t note_end;
	uint64_t segment_end;
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

		note_end = onset + duration;
		if (note_end <= group_start)
			continue;
		if (onset >= group_end)
			break;

		if (writer.position < onset &&
		    ml_abc_write_rest(&writer,
				      (onset < group_end ? onset : group_end) -
				      writer.position) < 0)
			return -1;

		if (writer.position == group_end)
			break;

		segment_end = note_end < group_end ? note_end : group_end;
		if (segment_end > writer.position &&
		    ml_abc_write_event(&writer, pitch,
				       segment_end - writer.position,
				       note_end > segment_end) < 0)
			return -1;
	}

	if (writer.position < group_end &&
	    ml_abc_write_rest(&writer, group_end - writer.position) < 0)
		return -1;

	return ml_abc_finish_voice(&writer);
}

static int
ml_abc_write_group(FILE *output,
		   const struct ml_note_sequence *notes,
		   uint64_t group_start,
		   unsigned int measures)
{
	struct ml_abc_writer writer = {
		.output = output,
		.position = group_start,
	};
	uint64_t group_end;

	group_end = group_start +
		    (uint64_t)measures * ML_ABC_UNITS_PER_BAR;

	if (fputs("V: Vocal\n", output) == EOF ||
	    ml_abc_write_vocal_group(output, notes,
				     group_start, group_end) < 0 ||
	    fputs("\nV: Ins\n", output) == EOF ||
	    ml_abc_write_full_rest(&writer, measures) < 0 ||
	    ml_abc_finish_voice(&writer) < 0 ||
	    fputc('\n', output) == EOF)
		return -1;

	return 0;
}

int
ml_write_abc(FILE *output, const struct ml_note_sequence *notes)
{
	uint64_t total_units = 0;
	uint64_t total_measures;
	uint64_t measure = 0;
	uint64_t remaining;
	uint64_t group_start;
	unsigned int group_measures;

	if (!output || !notes)
		return -1;

	if (ml_validate_abc(notes, &total_units) < 0)
		return -1;

	total_measures = total_units / ML_ABC_UNITS_PER_BAR;
	if (total_units % ML_ABC_UNITS_PER_BAR)
		++total_measures;
	if (!total_measures)
		total_measures = 1;

	if (total_measures > UINT64_MAX / ML_ABC_UNITS_PER_BAR)
		return -1;

	if (fputs("X:1\n"
		  "T:\n"
		  "M:4/4\n"
		  "L:1/32\n"
		  "Q:1/4=120\n"
		  "V: Vocal clef=treble name=\"Vocal Melody\" snm=\"Vocal\"\n"
		  "V: Ins clef=treble name=\"Ins Melody\" snm=\"Inst.\"\n"
		  "K:C\n",
		  output) == EOF)
		return -1;

	while (measure < total_measures) {
		remaining = total_measures - measure;
		group_measures = remaining < ML_ABC_MAX_GROUP_MEASURES ?
				 (unsigned int)remaining :
				 ML_ABC_MAX_GROUP_MEASURES;
		group_start = measure * ML_ABC_UNITS_PER_BAR;

		if (ml_abc_write_group(output, notes,
				       group_start, group_measures) < 0)
			return -1;

		measure += group_measures;
	}

	if (ferror(output))
		return -1;

	/* The caller owns the stream and is responsible for flushing it. */
	return 0;
}
