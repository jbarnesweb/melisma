#include <stdlib.h>

#include "melisma.h"

static int
ml_binding_append(struct ml_binding_sequence *bindings,
		  enum ml_binding_type type,
		  const struct ml_syllable *syllable)
{
	struct ml_binding *new_bindings;
	size_t capacity;

	if (bindings->count == bindings->capacity) {
		capacity = bindings->capacity ? bindings->capacity * 2 : 8;

		new_bindings = realloc(bindings->bindings,
				       capacity * sizeof(*new_bindings));
		if (!new_bindings)
			return -1;

		bindings->bindings = new_bindings;
		bindings->capacity = capacity;
	}

	bindings->bindings[bindings->count].type = type;
	bindings->bindings[bindings->count].syllable = syllable;
	++bindings->count;

	return 0;
}

void
ml_binding_sequence_destroy(struct ml_binding_sequence *bindings)
{
	free(bindings->bindings);

	bindings->bindings = NULL;
	bindings->count = 0;
	bindings->capacity = 0;
}

int
ml_bind(const struct ml_phrase *phrase,
	const struct ml_note_sequence *notes,
	struct ml_binding_sequence *bindings)
{
	const struct ml_element *element;
	const struct ml_syllable *syllable;
	size_t required;
	size_t i;
	unsigned int j;

	bindings->bindings = NULL;
	bindings->count = 0;
	bindings->capacity = 0;

	required = 0;

	for (i = 0; i < phrase->count; ++i) {
		element = &phrase->elements[i];

		if (element->type == ML_ELEMENT_UNTEXTED) {
			++required;
			continue;
		}

		syllable = &element->syllable;
		required += 1 + syllable->continuations;
	}

	if (required != notes->count)
		return -1;

	for (i = 0; i < phrase->count; ++i) {
		element = &phrase->elements[i];

		if (element->type == ML_ELEMENT_UNTEXTED) {
			if (ml_binding_append(bindings,
					      ML_BINDING_UNTEXTED,
					      NULL) < 0)
				goto error;

			continue;
		}

		syllable = &element->syllable;

		if (ml_binding_append(bindings,
				      ML_BINDING_ONSET,
				      syllable) < 0)
			goto error;

		for (j = 0; j < syllable->continuations; ++j) {
			if (ml_binding_append(bindings,
					      ML_BINDING_CONTINUATION,
					      syllable) < 0)
				goto error;
		}
	}

	return 0;

error:
	ml_binding_sequence_destroy(bindings);
	return -1;
}

