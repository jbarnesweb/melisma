#ifndef MELISMA_H
#define MELISMA_H

#include <stddef.h>

struct ml_string {
	char *data;
	size_t length;
};

struct ml_syllable {
	struct ml_string text;
	unsigned int continuations;
};

enum ml_element_type {
	ML_ELEMENT_SYLLABLE,
	ML_ELEMENT_UNTEXTED,
};

struct ml_element {
	enum ml_element_type type;
	struct ml_syllable syllable;
};

struct ml_phrase {
	struct ml_element *elements;
	size_t count;
	size_t capacity;
};

struct ml_note_sequence {
	size_t count;
};

enum ml_binding_type {
	ML_BINDING_ONSET,
	ML_BINDING_CONTINUATION,
	ML_BINDING_UNTEXTED,
};

struct ml_binding {
	enum ml_binding_type type;
	const struct ml_syllable *syllable;
};

struct ml_binding_sequence {
	struct ml_binding *bindings;
	size_t count;
	size_t capacity;
};

void ml_phrase_init(struct ml_phrase *phrase);

int ml_phrase_append(struct ml_phrase *phrase, struct ml_element element);

void ml_phrase_destroy(struct ml_phrase *phrase);

int ml_bind(const struct ml_phrase *phrase,
	const struct ml_note_sequence *notes,
	struct ml_binding_sequence *bindings);

void ml_binding_sequence_destroy(struct ml_binding_sequence *bindings);

#endif /* MELISMA_H */
