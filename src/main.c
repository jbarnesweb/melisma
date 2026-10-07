#include <stdio.h>

#include "melisma_parser.tab.h"
#include "melisma_lexer.h"
#include "melisma.h"

int main(int argc, char **argv)
{
	yyscan_t scanner;
	struct ml_phrase phrase;
	FILE *input;
	int ret;
	
	if (argc != 2) {
		fprintf(stderr, "usage: %s FILE\n", argv[0]);
		return 2;
	}

	input = fopen(argv[1], "r");
	if (!input) {
		perror(argv[1]);
		return 2;
	}

	ml_phrase_init(&phrase);

	if (yylex_init(&scanner) != 0) {
		fprintf(stderr, "failed to initialize scanner\n");
		fclose(input);
		return 2;
	}

	yyset_in(input, scanner);
	ret = yyparse(scanner, &phrase);

	if (ret == 0) {
		struct ml_note_sequence notes = { .count = 5 };
		struct ml_binding_sequence bindings;
		size_t i;

		if (ml_bind(&phrase, &notes, &bindings) < 0) {
			fprintf(stderr, "failed to bind phrase to notes\n");
			ret = 1;
		} else {
			for (i = 0; i < bindings.count; ++i) {
				const struct ml_binding *binding = &bindings.bindings[i];

				switch (binding->type) {
				case ML_BINDING_ONSET:
					printf("ONSET %.*s\n",
					       (int)binding->syllable->text.length,
					       binding->syllable->text.data);
					break;

				case ML_BINDING_CONTINUATION:
					printf("CONTINUATION %.*s\n",
					       (int)binding->syllable->text.length,
					       binding->syllable->text.data);
					break;

				case ML_BINDING_UNTEXTED:
					printf("UNTEXTED\n");
					break;
				}
			}

			ml_binding_sequence_destroy(&bindings);
		}
	}

	ml_phrase_destroy(&phrase);

	yylex_destroy(scanner);
	fclose(input);

	return ret ? 1 : 0;
}
