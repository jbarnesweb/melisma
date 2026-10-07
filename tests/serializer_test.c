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

	if (fflush(output) == EOF) {
		perror("fflush");
		return 0;
	}

	if (fseek(output, 0, SEEK_END) != 0) {
		perror("fseek");
		return 0;
	}

	size = ftell(output);
	if (size < 0) {
		perror("ftell");
		return 0;
	}

	if (fseek(output, 0, SEEK_SET) != 0) {
		perror("fseek");
		return 0;
	}

	length = (size_t)size;
	actual = malloc(length + 1);
	if (!actual) {
		perror("malloc");
		return 0;
	}

	if (fread(actual, 1, length, output) != length) {
		fprintf(stderr, "failed to read serialized output\n");
		free(actual);
		return 0;
	}

	actual[length] = '\0';
	matches = strcmp(actual, expected) == 0;
	if (!matches)
		fprintf(stderr, "unexpected serialized output:\n%s", actual);

	free(actual);
	return matches;
}

static int
test_five_note_binding(void)
{
	struct ml_syllable glo = {
		.text = { .data = "Glo", .length = 3 },
	};
	struct ml_syllable ri = {
		.text = { .data = "ri", .length = 2 },
	};
	struct ml_syllable a = {
		.text = { .data = "a", .length = 1 },
		.continuations = 2,
	};
	struct ml_element elements[] = {
		{ .type = ML_ELEMENT_SYLLABLE, .syllable = glo },
		{ .type = ML_ELEMENT_SYLLABLE, .syllable = ri },
		{ .type = ML_ELEMENT_SYLLABLE, .syllable = a },
	};
	struct ml_phrase phrase = {
		.elements = elements,
		.count = 3,
		.capacity = 3,
	};
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
	struct ml_binding_sequence bindings;
	const char *expected =
		"{\"type\":\"header\",\"version\":1,\"ticks_per_quarter\":480}\n"
		"{\"type\":\"note\",\"pitch\":60,\"onset\":0,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"Glo\"}\n"
		"{\"type\":\"note\",\"pitch\":62,\"onset\":480,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"ri\"}\n"
		"{\"type\":\"note\",\"pitch\":64,\"onset\":960,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"a\"}\n"
		"{\"type\":\"note\",\"pitch\":65,\"onset\":1440,\"duration\":480,\"binding\":\"continuation\",\"syllable\":\"a\"}\n"
		"{\"type\":\"note\",\"pitch\":67,\"onset\":1920,\"duration\":480,\"binding\":\"continuation\",\"syllable\":\"a\"}\n";
	FILE *output;
	int ret = 1;

	if (ml_bind(&phrase, &notes, &bindings) < 0) {
		fprintf(stderr, "five-note binding unexpectedly failed\n");
		return 1;
	}

	output = tmpfile();
	if (!output) {
		perror("tmpfile");
		goto out_bindings;
	}

	if (ml_write_bound_ir(output, &notes, &bindings) < 0) {
		fprintf(stderr, "five-note serialization unexpectedly failed\n");
		goto out_output;
	}

	if (!output_matches(output, expected))
		goto out_output;

	ret = 0;

out_output:
	fclose(output);
out_bindings:
	ml_binding_sequence_destroy(&bindings);
	return ret;
}

static int
test_untexted(void)
{
	struct ml_note note = {
		.pitch = 48,
		.onset = 240,
		.duration = 120,
	};
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
	const char *expected =
		"{\"type\":\"header\",\"version\":1,\"ticks_per_quarter\":960}\n"
		"{\"type\":\"note\",\"pitch\":48,\"onset\":240,\"duration\":120,\"binding\":\"untexted\",\"syllable\":null}\n";
	FILE *output;
	int ret = 1;

	output = tmpfile();
	if (!output) {
		perror("tmpfile");
		return 1;
	}

	if (ml_write_bound_ir(output, &notes, &bindings) < 0) {
		fprintf(stderr, "untexted serialization unexpectedly failed\n");
		goto out;
	}

	if (!output_matches(output, expected))
		goto out;

	ret = 0;

out:
	fclose(output);
	return ret;
}

static int
test_json_escaping(void)
{
	char text[] = {
		'"', '\\', '\b', '\f', '\n', '\r', '\t', 0x01,
		(char)0xc3, (char)0xa9,
	};
	struct ml_syllable syllable = {
		.text = { .data = text, .length = sizeof(text) },
	};
	struct ml_note note = {
		.pitch = 60,
		.duration = 1,
	};
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
	const char *expected =
		"{\"type\":\"header\",\"version\":1,\"ticks_per_quarter\":480}\n"
		"{\"type\":\"note\",\"pitch\":60,\"onset\":0,\"duration\":1,\"binding\":\"onset\",\"syllable\":\"\\\"\\\\\\b\\f\\n\\r\\t\\u0001\xc3\xa9\"}\n";
	FILE *output;
	int ret = 1;

	output = tmpfile();
	if (!output) {
		perror("tmpfile");
		return 1;
	}

	if (ml_write_bound_ir(output, &notes, &bindings) < 0) {
		fprintf(stderr, "escaping serialization unexpectedly failed\n");
		goto out;
	}

	if (!output_matches(output, expected))
		goto out;

	ret = 0;

out:
	fclose(output);
	return ret;
}

static int
expect_rejected(const struct ml_note_sequence *notes,
		const struct ml_binding_sequence *bindings,
		const char *name)
{
	FILE *output;
	int ret;

	output = tmpfile();
	if (!output) {
		perror("tmpfile");
		return 1;
	}

	ret = ml_write_bound_ir(output, notes, bindings);
	if (ret == 0) {
		fprintf(stderr, "%s input unexpectedly accepted\n", name);
		fclose(output);
		return 1;
	}

	if (!output_matches(output, "")) {
		fprintf(stderr, "%s input produced partial output\n", name);
		fclose(output);
		return 1;
	}

	fclose(output);
	return 0;
}

static int
test_invalid_input(void)
{
	struct ml_syllable syllable = {
		.text = { .data = "a", .length = 1 },
	};
	struct ml_note notes_data[2] = {
		{ .pitch = 60, .duration = 480 },
		{ .pitch = 62, .onset = 480, .duration = 480 },
	};
	struct ml_note_sequence notes = {
		.notes = notes_data,
		.count = 1,
		.ticks_per_quarter = 480,
	};
	struct ml_binding binding = {
		.type = ML_BINDING_ONSET,
		.note = &notes_data[0],
		.syllable = &syllable,
	};
	struct ml_binding_sequence bindings = {
		.bindings = &binding,
		.count = 1,
		.capacity = 1,
	};
	struct ml_note_sequence invalid_notes;
	struct ml_binding_sequence invalid_bindings;
	FILE *output;

	output = tmpfile();
	if (!output) {
		perror("tmpfile");
		return 1;
	}

	if (ml_write_bound_ir(NULL, &notes, &bindings) == 0 ||
	    ml_write_bound_ir(output, NULL, &bindings) == 0 ||
	    ml_write_bound_ir(output, &notes, NULL) == 0) {
		fprintf(stderr, "NULL input unexpectedly accepted\n");
		fclose(output);
		return 1;
	}
	fclose(output);

	invalid_notes = notes;
	invalid_notes.ticks_per_quarter = 0;
	if (expect_rejected(&invalid_notes, &bindings, "zero timebase"))
		return 1;

	invalid_bindings = bindings;
	invalid_bindings.count = 0;
	if (expect_rejected(&notes, &invalid_bindings, "count mismatch"))
		return 1;

	invalid_notes = notes;
	invalid_notes.notes = NULL;
	if (expect_rejected(&invalid_notes, &bindings, "missing notes array"))
		return 1;

	invalid_bindings = bindings;
	invalid_bindings.bindings = NULL;
	if (expect_rejected(&notes, &invalid_bindings,
			    "missing bindings array"))
		return 1;

	binding.note = &notes_data[1];
	if (expect_rejected(&notes, &bindings, "wrong note reference"))
		return 1;
	binding.note = &notes_data[0];

	binding.syllable = NULL;
	if (expect_rejected(&notes, &bindings, "onset without syllable"))
		return 1;

	binding.type = ML_BINDING_CONTINUATION;
	if (expect_rejected(&notes, &bindings,
			    "continuation without syllable"))
		return 1;

	binding.type = ML_BINDING_UNTEXTED;
	binding.syllable = &syllable;
	if (expect_rejected(&notes, &bindings, "untexted with syllable"))
		return 1;

	binding.type = (enum ml_binding_type)99;
	binding.syllable = NULL;
	if (expect_rejected(&notes, &bindings, "unknown binding type"))
		return 1;

	return 0;
}

int
main(void)
{
	if (test_five_note_binding())
		return 1;

	if (test_untexted())
		return 1;

	if (test_json_escaping())
		return 1;

	if (test_invalid_input())
		return 1;

	printf("serializer tests passed\n");
	return 0;
}
