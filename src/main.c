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
		size_t i;
	
		for (i = 0; i < phrase.count; ++i) {
			const struct ml_element *element = &phrase.elements[i];
	
			if (element->type == ML_ELEMENT_UNTEXTED) {
				printf("UNTEXTED\n");
				continue;
			}
	
			printf("SYLLABLE %.*s continuations=%u\n",
			       (int)element->syllable.text.length,
			       element->syllable.text.data,
			       element->syllable.continuations);
		}
	}

	ml_phrase_destroy(&phrase);

	yylex_destroy(scanner);
	fclose(input);

	return ret ? 1 : 0;
}
