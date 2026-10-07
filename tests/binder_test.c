#include <stdio.h>

#include "melisma.h"

static int
test_exact_binding(void)
{
	struct ml_syllable glo = {
		.text = { .data = "Glo", .length = 3 },
		.continuations = 0,
	};
	struct ml_syllable ri = {
		.text = { .data = "ri", .length = 2 },
		.continuations = 0,
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
	int ret;

	ret = ml_bind(&phrase, &notes, &bindings);
	if (ret < 0) {
		fprintf(stderr, "exact binding unexpectedly failed\n");
		return 1;
	}

	if (bindings.count != 5) {
		fprintf(stderr, "expected 5 bindings, got %zu\n",
			bindings.count);
		ml_binding_sequence_destroy(&bindings);
		return 1;
	}

	if (bindings.bindings[0].type != ML_BINDING_ONSET ||
	    bindings.bindings[0].syllable != &elements[0].syllable ||
	    bindings.bindings[1].type != ML_BINDING_ONSET ||
	    bindings.bindings[1].syllable != &elements[1].syllable ||
	    bindings.bindings[2].type != ML_BINDING_ONSET ||
	    bindings.bindings[2].syllable != &elements[2].syllable ||
	    bindings.bindings[3].type != ML_BINDING_CONTINUATION ||
	    bindings.bindings[3].syllable != &elements[2].syllable ||
	    bindings.bindings[4].type != ML_BINDING_CONTINUATION ||
	    bindings.bindings[4].syllable != &elements[2].syllable) {
		fprintf(stderr, "binding sequence does not match expected result\n");
		ml_binding_sequence_destroy(&bindings);
		return 1;
	}

	ml_binding_sequence_destroy(&bindings);
	return 0;
}

static int
test_too_few_notes(void)
{
	struct ml_syllable syllable = {
		.text = { .data = "a", .length = 1 },
		.continuations = 2,
	};
	struct ml_element element = {
		.type = ML_ELEMENT_SYLLABLE,
		.syllable = syllable,
	};
	struct ml_phrase phrase = {
		.elements = &element,
		.count = 1,
		.capacity = 1,
	};
	struct ml_note_sequence notes = { .count = 2 };
	struct ml_binding_sequence bindings;

	if (ml_bind(&phrase, &notes, &bindings) == 0) {
		fprintf(stderr, "too-few-notes binding unexpectedly succeeded\n");
		ml_binding_sequence_destroy(&bindings);
		return 1;
	}

	return 0;
}

static int
test_too_many_notes(void)
{
	struct ml_syllable syllable = {
		.text = { .data = "a", .length = 1 },
		.continuations = 2,
	};
	struct ml_element element = {
		.type = ML_ELEMENT_SYLLABLE,
		.syllable = syllable,
	};
	struct ml_phrase phrase = {
		.elements = &element,
		.count = 1,
		.capacity = 1,
	};
	struct ml_note_sequence notes = { .count = 4 };
	struct ml_binding_sequence bindings;

	if (ml_bind(&phrase, &notes, &bindings) == 0) {
		fprintf(stderr, "too-many-notes binding unexpectedly succeeded\n");
		ml_binding_sequence_destroy(&bindings);
		return 1;
	}

	return 0;
}

static int
test_untexted(void)
{
	struct ml_element element = {
		.type = ML_ELEMENT_UNTEXTED,
	};
	struct ml_phrase phrase = {
		.elements = &element,
		.count = 1,
		.capacity = 1,
	};
	struct ml_note_sequence notes = { .count = 1 };
	struct ml_binding_sequence bindings;
	int ret;

	ret = ml_bind(&phrase, &notes, &bindings);
	if (ret < 0) {
		fprintf(stderr, "untexted binding unexpectedly failed\n");
		return 1;
	}

	if (bindings.count != 1 ||
	    bindings.bindings[0].type != ML_BINDING_UNTEXTED ||
	    bindings.bindings[0].syllable != NULL) {
		fprintf(stderr, "untexted binding does not match expected result\n");
		ml_binding_sequence_destroy(&bindings);
		return 1;
	}

	ml_binding_sequence_destroy(&bindings);
	return 0;
}

int
main(void)
{
	if (test_exact_binding())
		return 1;

	if (test_too_few_notes())
		return 1;

	if (test_too_many_notes())
		return 1;

	if (test_untexted())
		return 1;

	printf("binder tests passed\n");
	return 0;
}
