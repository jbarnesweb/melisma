#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "melisma.h"

static const char abc_header[] =
	"X:1\n"
	"T:\n"
	"M:4/4\n"
	"L:1/32\n"
	"Q:1/4=120\n"
	"V: Vocal clef=treble name=\"Vocal Melody\" snm=\"Vocal\"\n"
	"V: Ins clef=treble name=\"Ins Melody\" snm=\"Inst.\"\n"
	"K:C\n"
	"\n";

static int
output_matches(FILE *output, const char *expected)
{
	char actual[2048];
	size_t length;
	int extra;

	if (fflush(output) == EOF ||
	    fseek(output, 0, SEEK_SET) != 0)
		return 0;

	length = strlen(expected);
	if (length >= sizeof(actual) ||
	    fread(actual, 1, length, output) != length)
		return 0;

	extra = fgetc(output);
	return extra == EOF && memcmp(actual, expected, length) == 0;
}

static int
expect_output(const char *name,
	      const struct ml_note_sequence *notes,
	      const char *vocal,
	      const char *instrument)
{
	char expected[2048];
	FILE *output;
	int length;
	int ret = 1;

	length = snprintf(expected, sizeof(expected),
			  "%sV: Vocal\n%s\n\nV: Ins\n%s\n",
			  abc_header, vocal, instrument);
	if (length < 0 || (size_t)length >= sizeof(expected))
		return 1;

	output = tmpfile();
	if (!output) {
		perror("tmpfile");
		return 1;
	}

	if (ml_write_abc(output, notes) < 0) {
		fprintf(stderr, "%s unexpectedly failed\n", name);
		goto out;
	}

	if (!output_matches(output, expected)) {
		fprintf(stderr, "%s produced unexpected ABC\n", name);
		goto out;
	}

	ret = 0;

out:
	fclose(output);
	return ret;
}

static int
test_five_notes(void)
{
	struct ml_note notes_data[] = {
		{ .pitch = 60, .onset = 0,    .duration = 480 },
		{ .pitch = 62, .onset = 480,  .duration = 480 },
		{ .pitch = 64, .onset = 960,  .duration = 480 },
		{ .pitch = 65, .onset = 1440, .duration = 480 },
		{ .pitch = 67, .onset = 1920, .duration = 480 },
	};
	struct ml_note_sequence notes = {
		.notes = notes_data,
		.count = 5,
		.ticks_per_quarter = 480,
	};

	return expect_output("five notes", &notes,
			     "=C8 =D8 =E8 =F8 | =G8",
			     "z32 | z8");
}

static int
test_rests_and_bars(void)
{
	struct ml_note rest_notes[] = {
		{ .pitch = 60, .onset = 0,   .duration = 480 },
		{ .pitch = 62, .onset = 960, .duration = 480 },
	};
	struct ml_note crossing_note = {
		.pitch = 60,
		.onset = 1440,
		.duration = 960,
	};
	struct ml_note crossing_rest[] = {
		{ .pitch = 60, .onset = 0,    .duration = 480 },
		{ .pitch = 62, .onset = 2400, .duration = 480 },
	};
	struct ml_note_sequence notes = {
		.notes = rest_notes,
		.count = 2,
		.ticks_per_quarter = 480,
	};

	if (expect_output("rest between notes", &notes,
			  "=C8 z8 =D8", "z24"))
		return 1;

	notes.notes = &crossing_note;
	notes.count = 1;
	if (expect_output("note crossing bar", &notes,
			  "z24 =C8- | =C8", "z32 | z8"))
		return 1;

	notes.notes = crossing_rest;
	notes.count = 2;
	if (expect_output("rest crossing bar", &notes,
			  "=C8 z24 | z8 =D8", "z32 | z16"))
		return 1;

	return 0;
}

static int
test_pitch_spelling(void)
{
	struct ml_note notes_data[] = {
		{ .pitch = 59, .onset = 0,   .duration = 60 },
		{ .pitch = 60, .onset = 60,  .duration = 60 },
		{ .pitch = 61, .onset = 120, .duration = 60 },
		{ .pitch = 71, .onset = 180, .duration = 60 },
		{ .pitch = 72, .onset = 240, .duration = 60 },
		{ .pitch = 73, .onset = 300, .duration = 60 },
		{ .pitch = 84, .onset = 360, .duration = 60 },
	};
	struct ml_note_sequence notes = {
		.notes = notes_data,
		.count = 7,
		.ticks_per_quarter = 480,
	};

	return expect_output("pitch spelling", &notes,
			     "=B,1 =C1 ^C1 =B1 =c1 ^c1 =c'1",
			     "z7");
}

static int
test_accidental_cancellation(void)
{
	struct ml_note notes_data[] = {
		{ .pitch = 61, .onset = 0,  .duration = 60 },
		{ .pitch = 60, .onset = 60, .duration = 60 },
	};
	struct ml_note_sequence notes = {
		.notes = notes_data,
		.count = 2,
		.ticks_per_quarter = 480,
	};

	return expect_output("accidental cancellation", &notes,
			     "^C1 =C1", "z2");
}

static int
test_pitch_boundaries(void)
{
	struct ml_note notes_data[] = {
		{ .pitch = 0,   .onset = 0,   .duration = 60 },
		{ .pitch = 47,  .onset = 60,  .duration = 60 },
		{ .pitch = 48,  .onset = 120, .duration = 60 },
		{ .pitch = 49,  .onset = 180, .duration = 60 },
		{ .pitch = 59,  .onset = 240, .duration = 60 },
		{ .pitch = 60,  .onset = 300, .duration = 60 },
		{ .pitch = 61,  .onset = 360, .duration = 60 },
		{ .pitch = 71,  .onset = 420, .duration = 60 },
		{ .pitch = 72,  .onset = 480, .duration = 60 },
		{ .pitch = 73,  .onset = 540, .duration = 60 },
		{ .pitch = 127, .onset = 600, .duration = 60 },
	};
	struct ml_note_sequence notes = {
		.notes = notes_data,
		.count = 11,
		.ticks_per_quarter = 480,
	};

	return expect_output("pitch boundaries", &notes,
			     "=C,,,,,1 =B,,1 =C,1 ^C,1 =B,1 =C1 ^C1 "
			     "=B1 =c1 ^c1 =g''''1",
			     "z11");
}

static int
test_bar_boundaries(void)
{
	struct ml_note ending_note = {
		.pitch = 60,
		.duration = 1920,
	};
	struct ml_note starting_note = {
		.pitch = 60,
		.onset = 1920,
		.duration = 480,
	};
	struct ml_note long_note = {
		.pitch = 60,
		.onset = 1440,
		.duration = 4800,
	};
	struct ml_note long_rest[] = {
		{ .pitch = 60, .onset = 0,    .duration = 480 },
		{ .pitch = 62, .onset = 6240, .duration = 480 },
	};
	struct ml_note_sequence notes = {
		.notes = &ending_note,
		.count = 1,
		.ticks_per_quarter = 480,
	};

	if (expect_output("note ending at barline", &notes,
			  "=C32 |", "z32 |"))
		return 1;

	notes.notes = &starting_note;
	if (expect_output("note starting at barline", &notes,
			  "z32 | =C8", "z32 | z8"))
		return 1;

	notes.notes = &long_note;
	if (expect_output("note crossing multiple bars", &notes,
			  "z24 =C8- | =C32- | =C32- | =C8",
			  "z32 | z32 | z32 | z8"))
		return 1;

	notes.notes = long_rest;
	notes.count = 2;
	if (expect_output("rest crossing multiple bars", &notes,
			  "=C8 z24 | z32 | z32 | z8 =D8",
			  "z32 | z32 | z32 | z16"))
		return 1;

	return 0;
}

static int
test_durations_and_empty(void)
{
	static const uint64_t units[] = { 1, 2, 4, 8, 16, 32 };
	static const char *const vocal[] = {
		"=C1", "=C2", "=C4", "=C8", "=C16", "=C32 |",
	};
	static const char *const instrument[] = {
		"z1", "z2", "z4", "z8", "z16", "z32 |",
	};
	struct ml_note note = {
		.pitch = 60,
	};
	struct ml_note_sequence notes = {
		.notes = &note,
		.count = 1,
		.ticks_per_quarter = 480,
	};
	size_t i;

	for (i = 0; i < sizeof(units) / sizeof(units[0]); ++i) {
		note.duration = units[i] * 60;
		if (expect_output("duration suffix", &notes,
				  vocal[i], instrument[i]))
			return 1;
	}

	note.duration = 480;
	notes.notes = &note;
	notes.count = 1;
	if (expect_output("adjacent notes setup", &notes,
			  "=C8", "z8"))
		return 1;

	notes.notes = NULL;
	notes.count = 0;
	if (expect_output("empty sequence", &notes, "", ""))
		return 1;

	return 0;
}

static int
test_adjacent_notes(void)
{
	struct ml_note notes_data[] = {
		{ .pitch = 60, .onset = 0,   .duration = 480 },
		{ .pitch = 62, .onset = 480, .duration = 480 },
	};
	struct ml_note_sequence notes = {
		.notes = notes_data,
		.count = 2,
		.ticks_per_quarter = 480,
	};

	return expect_output("adjacent notes", &notes,
			     "=C8 =D8", "z16");
}

static int
test_large_timing(void)
{
	struct ml_note note = {
		.pitch = 60,
		.onset = (uint64_t)UINT32_MAX * 2,
		.duration = UINT32_MAX,
	};
	struct ml_note_sequence notes = {
		.notes = &note,
		.count = 1,
		.ticks_per_quarter = UINT32_MAX,
	};

	return expect_output("large exact timing", &notes,
			     "z16 =C8", "z24");
}

static int
expect_rejected(const char *name, const struct ml_note_sequence *notes)
{
	FILE *output;
	long size;
	int ret = 1;

	output = tmpfile();
	if (!output) {
		perror("tmpfile");
		return 1;
	}

	if (ml_write_abc(output, notes) == 0) {
		fprintf(stderr, "%s unexpectedly succeeded\n", name);
		goto out;
	}

	if (fflush(output) == EOF ||
	    fseek(output, 0, SEEK_END) != 0 ||
	    (size = ftell(output)) != 0) {
		fprintf(stderr, "%s produced partial output\n", name);
		goto out;
	}

	ret = 0;

out:
	fclose(output);
	return ret;
}

static int
test_invalid_input(void)
{
	struct ml_note notes_data[] = {
		{ .pitch = 60, .onset = 0,   .duration = 480 },
		{ .pitch = 62, .onset = 480, .duration = 480 },
	};
	struct ml_note_sequence notes = {
		.notes = notes_data,
		.count = 2,
		.ticks_per_quarter = 480,
	};
	struct ml_note_sequence invalid;
	FILE *output;

	output = tmpfile();
	if (!output) {
		perror("tmpfile");
		return 1;
	}
	if (ml_write_abc(NULL, &notes) == 0 ||
	    ml_write_abc(output, NULL) == 0) {
		fprintf(stderr, "NULL input unexpectedly accepted\n");
		fclose(output);
		return 1;
	}
	fclose(output);

	invalid = notes;
	invalid.ticks_per_quarter = 0;
	if (expect_rejected("zero TPQ", &invalid))
		return 1;

	invalid = notes;
	invalid.notes[0].duration = 1;
	if (expect_rejected("non-integral timing", &invalid))
		return 1;
	invalid.notes[0].duration = 480;

	invalid = notes;
	invalid.notes[0].onset = 1;
	if (expect_rejected("non-integral onset", &invalid))
		return 1;
	invalid.notes[0].onset = 0;

	invalid = notes;
	invalid.notes[0].duration = 0;
	if (expect_rejected("zero duration", &invalid))
		return 1;
	invalid.notes[0].duration = 480;

	invalid = notes;
	invalid.notes[1].onset = 240;
	if (expect_rejected("overlap", &invalid))
		return 1;

	invalid.notes[0].onset = 480;
	invalid.notes[1].onset = 0;
	if (expect_rejected("non-monotonic onset", &invalid))
		return 1;
	invalid.notes[0].onset = 0;
	invalid.notes[1].onset = 480;

	invalid.notes[0].pitch = 128;
	if (expect_rejected("out-of-range pitch", &invalid))
		return 1;
	invalid.notes[0].pitch = 60;

	invalid.notes = NULL;
	if (expect_rejected("missing note array", &invalid))
		return 1;

	invalid = notes;
	invalid.count = 1;
	invalid.notes[0].onset = UINT64_MAX - 10;
	invalid.notes[0].duration = 16;
	invalid.ticks_per_quarter = 1;
	if (expect_rejected("tick end overflow", &invalid))
		return 1;

	invalid.notes[0].onset = UINT64_MAX / 8 + 1;
	invalid.notes[0].duration = 1;
	if (expect_rejected("ABC unit overflow", &invalid))
		return 1;

	invalid.notes[0].onset = 0;
	invalid.notes[0].duration = 480;

	return 0;
}

static int
test_output_failure(void)
{
	struct ml_note note = {
		.pitch = 60,
		.duration = 480,
	};
	struct ml_note_sequence notes = {
		.notes = &note,
		.count = 1,
		.ticks_per_quarter = 480,
	};
	FILE *output;

	output = fopen("/dev/full", "w");
	if (!output) {
		perror("/dev/full");
		return 1;
	}

	if (setvbuf(output, NULL, _IONBF, 0) != 0) {
		fclose(output);
		return 1;
	}

	if (ml_write_abc(output, &notes) == 0) {
		fprintf(stderr, "output failure unexpectedly succeeded\n");
		fclose(output);
		return 1;
	}

	fclose(output);
	return 0;
}

int
main(void)
{
	if (test_five_notes() ||
	    test_rests_and_bars() ||
	    test_pitch_spelling() ||
	    test_accidental_cancellation() ||
	    test_pitch_boundaries() ||
	    test_bar_boundaries() ||
	    test_durations_and_empty() ||
	    test_adjacent_notes() ||
	    test_large_timing() ||
	    test_invalid_input() ||
	    test_output_failure())
		return 1;

	printf("ABC serializer tests passed\n");
	return 0;
}
