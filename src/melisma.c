#include <stddef.h>
#include <stdlib.h>

#include "melisma_parser.tab.h"
#include "melisma_lexer.h"
#include "melisma.h"

void
ml_phrase_init(struct ml_phrase *phrase)
{
	phrase->elements = NULL;
	phrase->count = 0;
	phrase->capacity = 0;
}

int
ml_phrase_append(struct ml_phrase *phrase, struct ml_element element)
{
	struct ml_element *elements;
	size_t capacity;

	if (phrase->count == phrase->capacity) {
		capacity = phrase->capacity ? phrase->capacity * 2 : 8;

		elements = realloc(phrase->elements,
				   capacity * sizeof(*elements));
		if (!elements)
			return -1;

		phrase->elements = elements;
		phrase->capacity = capacity;
	}

	phrase->elements[phrase->count++] = element;
	return 0;
}

void
ml_phrase_destroy(struct ml_phrase *phrase)
{
	size_t i;

	for (i = 0; i < phrase->count; ++i) {
		if (phrase->elements[i].type == ML_ELEMENT_SYLLABLE)
			free(phrase->elements[i].syllable.text.data);
	}

	free(phrase->elements);

	phrase->elements = NULL;
	phrase->count = 0;
	phrase->capacity = 0;
}

int
ml_parse_file(FILE *input, struct ml_phrase *phrase)
{
	yyscan_t scanner;
	int ret;

	if (yylex_init(&scanner) != 0)
		return -1;

	yyset_in(input, scanner);
	ret = yyparse(scanner, phrase);

	yylex_destroy(scanner);

	return ret;
}

