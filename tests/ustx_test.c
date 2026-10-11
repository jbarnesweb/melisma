#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "melisma.h"

static int
output_matches(FILE *output, const char *expected)
{
	char *actual;
	long size;
	size_t length;
	int matches;

	if (fflush(output) == EOF ||
	    fseek(output, 0, SEEK_END) != 0 ||
	    (size = ftell(output)) < 0 ||
	    fseek(output, 0, SEEK_SET) != 0)
		return 0;

	length = (size_t)size;
	actual = malloc(length + 1);
	if (!actual)
		return 0;

	if (fread(actual, 1, length, output) != length) {
		free(actual);
		return 0;
	}

	actual[length] = '\0';
	matches = strcmp(actual, expected) == 0;
	if (!matches)
		fprintf(stderr, "unexpected USTX output:\n%s", actual);
	free(actual);
	return matches;
}

static int
configured_output_contains(const struct ml_ustx_config *config,
			   const char *expected)
{
	struct ml_syllable syllable = {
		.text = { .data = "a", .length = 1 },
	};
	struct ml_note note = { .pitch = 60, .duration = 480 };
	struct ml_note_sequence notes = {
		.notes = &note,
		.count = 1,
		.ticks_per_quarter = 480,
	};
	struct ml_binding binding = {
		.type = ML_BINDING_ONSET,
		.note = &note,
		.syllable = &syllable,
	};
	struct ml_binding_sequence bindings = {
		.bindings = &binding,
		.count = 1,
		.capacity = 1,
	};
	char data[4096];
	FILE *output;
	size_t length;
	int contains;

	output = tmpfile();
	if (!output)
		return 0;

	if (ml_write_ustx(output, &notes, &bindings, config) < 0 ||
	    fflush(output) == EOF ||
	    fseek(output, 0, SEEK_SET) != 0) {
		fclose(output);
		return 0;
	}

	length = fread(data, 1, sizeof(data) - 1, output);
	if (ferror(output)) {
		fclose(output);
		return 0;
	}
	data[length] = '\0';
	contains = strstr(data, expected) != NULL;
	fclose(output);
	return contains;
}

static int
test_gloria(void)
{
	struct ml_ustx_config config = { "", "", "" };
	struct ml_syllable syllables[] = {
		{ .text = { .data = "Glo", .length = 3 } },
		{ .text = { .data = "ri", .length = 2 } },
		{ .text = { .data = "a", .length = 1 } },
	};
	struct ml_note note_data[] = {
		{ .pitch = 60, .onset = 0, .duration = 480 },
		{ .pitch = 62, .onset = 480, .duration = 480 },
		{ .pitch = 64, .onset = 960, .duration = 480 },
		{ .pitch = 65, .onset = 1440, .duration = 480 },
		{ .pitch = 67, .onset = 1920, .duration = 480 },
	};
	struct ml_note_sequence notes = {
		.notes = note_data,
		.count = 5,
		.ticks_per_quarter = 480,
	};
	struct ml_binding binding_data[] = {
		{ ML_BINDING_ONSET, &note_data[0], &syllables[0] },
		{ ML_BINDING_ONSET, &note_data[1], &syllables[1] },
		{ ML_BINDING_ONSET, &note_data[2], &syllables[2] },
		{ ML_BINDING_CONTINUATION, &note_data[3], &syllables[2] },
		{ ML_BINDING_CONTINUATION, &note_data[4], &syllables[2] },
	};
	struct ml_binding_sequence bindings = {
		.bindings = binding_data,
		.count = 5,
		.capacity = 5,
	};
	const char *expected =
		"name: Melisma\n"
		"comment: \"\"\n"
		"ustx_version: \"0.7\"\n"
		"resolution: 480\n"
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
		"- singer: \"\"\n"
		"  phonemizer: \"\"\n"
		"  renderer_settings:\n"
		"    renderer: \"\"\n"
		"  track_name: Track1\n"
		"voice_parts:\n"
		"- duration: 2400\n"
		"  name: New Part\n"
		"  comment: \"\"\n"
		"  track_no: 0\n"
		"  position: 0\n"
		"  notes:\n"
		"  - position: 0\n"
		"    duration: 480\n"
		"    tone: 60\n"
		"    lyric: \"Glo\"\n"
		"    pitch:\n"
		"      data:\n"
		"      - {x: -40, y: 0, shape: io}\n"
		"      - {x: 40, y: 0, shape: io}\n"
		"      snap_first: true\n"
		"    vibrato: {length: 0, period: 175, depth: 25, in: 10, out: 10, shift: 0, drift: 0, vol_link: 0}\n"
		"    phoneme_expressions: []\n"
		"    phoneme_overrides: []\n"
		"  - position: 480\n"
		"    duration: 480\n"
		"    tone: 62\n"
		"    lyric: \"ri\"\n"
		"    pitch:\n"
		"      data:\n"
		"      - {x: -40, y: 0, shape: io}\n"
		"      - {x: 40, y: 0, shape: io}\n"
		"      snap_first: true\n"
		"    vibrato: {length: 0, period: 175, depth: 25, in: 10, out: 10, shift: 0, drift: 0, vol_link: 0}\n"
		"    phoneme_expressions: []\n"
		"    phoneme_overrides: []\n"
		"  - position: 960\n"
		"    duration: 480\n"
		"    tone: 64\n"
		"    lyric: \"a\"\n"
		"    pitch:\n"
		"      data:\n"
		"      - {x: -40, y: 0, shape: io}\n"
		"      - {x: 40, y: 0, shape: io}\n"
		"      snap_first: true\n"
		"    vibrato: {length: 0, period: 175, depth: 25, in: 10, out: 10, shift: 0, drift: 0, vol_link: 0}\n"
		"    phoneme_expressions: []\n"
		"    phoneme_overrides: []\n"
		"  - position: 1440\n"
		"    duration: 480\n"
		"    tone: 65\n"
		"    lyric: \"+\"\n"
		"    pitch:\n"
		"      data:\n"
		"      - {x: -40, y: 0, shape: io}\n"
		"      - {x: 40, y: 0, shape: io}\n"
		"      snap_first: true\n"
		"    vibrato: {length: 0, period: 175, depth: 25, in: 10, out: 10, shift: 0, drift: 0, vol_link: 0}\n"
		"    phoneme_expressions: []\n"
		"    phoneme_overrides: []\n"
		"  - position: 1920\n"
		"    duration: 480\n"
		"    tone: 67\n"
		"    lyric: \"+\"\n"
		"    pitch:\n"
		"      data:\n"
		"      - {x: -40, y: 0, shape: io}\n"
		"      - {x: 40, y: 0, shape: io}\n"
		"      snap_first: true\n"
		"    vibrato: {length: 0, period: 175, depth: 25, in: 10, out: 10, shift: 0, drift: 0, vol_link: 0}\n"
		"    phoneme_expressions: []\n"
		"    phoneme_overrides: []\n"
		"  curves: []\n"
		"wave_parts: []\n";
	FILE *output = tmpfile();
	int result;

	if (!output)
		return 1;
	result = ml_write_ustx(output, &notes, &bindings, &config) < 0 ||
		 !output_matches(output, expected);
	fclose(output);
	return result;
}

static int
test_untexted_and_validation(void)
{
	struct ml_ustx_config config = { "", "", "" };
	struct ml_note note = { .pitch = 48, .onset = 240, .duration = 120 };
	struct ml_note_sequence notes = {
		.notes = &note,
		.count = 1,
		.ticks_per_quarter = 960,
	};
	struct ml_binding binding = {
		.type = ML_BINDING_UNTEXTED,
		.note = &note,
	};
	struct ml_binding_sequence bindings = {
		.bindings = &binding,
		.count = 1,
		.capacity = 1,
	};
	FILE *output = tmpfile();
	int result = 1;

	if (!output)
		return 1;

	if (ml_write_ustx(output, &notes, &bindings, &config) < 0 ||
	    !output_matches(output,
		"name: Melisma\n"
		"comment: \"\"\n"
		"ustx_version: \"0.7\"\n"
		"resolution: 960\n"
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
		"- singer: \"\"\n"
		"  phonemizer: \"\"\n"
		"  renderer_settings:\n"
		"    renderer: \"\"\n"
		"  track_name: Track1\n"
		"voice_parts:\n"
		"- duration: 360\n"
		"  name: New Part\n"
		"  comment: \"\"\n"
		"  track_no: 0\n"
		"  position: 0\n"
		"  notes:\n"
		"  - position: 240\n"
		"    duration: 120\n"
		"    tone: 48\n"
		"    lyric: \"\"\n"
		"    pitch:\n"
		"      data:\n"
		"      - {x: -40, y: 0, shape: io}\n"
		"      - {x: 40, y: 0, shape: io}\n"
		"      snap_first: true\n"
		"    vibrato: {length: 0, period: 175, depth: 25, in: 10, out: 10, shift: 0, drift: 0, vol_link: 0}\n"
		"    phoneme_expressions: []\n"
		"    phoneme_overrides: []\n"
		"  curves: []\n"
		"wave_parts: []\n"))
		goto out;

	rewind(output);
	note.onset = INT_MAX;
	if (ml_write_ustx(output, &notes, &bindings, &config) == 0)
		goto out;

	result = 0;
out:
	fclose(output);
	return result;
}

static int
test_track_configuration(void)
{
	struct ml_ustx_config tiger = {
		.singer = "TIGER_DS_v102",
		.phonemizer =
			"OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer",
		.renderer = "DIFFSINGER",
	};
	struct ml_ustx_config escaped = {
		.singer = "Tiger: \"A\"\\B",
		.phonemizer = "Class\nName",
		.renderer = "#world",
	};
	struct ml_ustx_config invalid = { NULL, "", "" };

	if (!configured_output_contains(
		    &tiger,
		    "tracks:\n"
		    "- singer: \"TIGER_DS_v102\"\n"
		    "  phonemizer: \"OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer\"\n"
		    "  renderer_settings:\n"
		    "    renderer: \"DIFFSINGER\"\n"
		    "  track_name: Track1\n")) {
		fprintf(stderr, "TIGER track configuration did not match\n");
		return 1;
	}

	if (!configured_output_contains(
		    &escaped,
		    "tracks:\n"
		    "- singer: \"Tiger: \\\"A\\\"\\\\B\"\n"
		    "  phonemizer: \"Class\\nName\"\n"
		    "  renderer_settings:\n"
		    "    renderer: \"#world\"\n"
		    "  track_name: Track1\n")) {
		fprintf(stderr, "escaped track configuration did not match\n");
		return 1;
	}

	if (configured_output_contains(&invalid, "tracks:")) {
		fprintf(stderr, "NULL track configuration unexpectedly accepted\n");
		return 1;
	}

	return 0;
}

int
main(void)
{
	if (test_gloria() ||
	    test_untexted_and_validation() ||
	    test_track_configuration())
		return 1;

	printf("USTX serializer tests passed\n");
	return 0;
}
