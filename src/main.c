#include <stdio.h>

#include "melisma.h"

int main(int argc, char **argv)
{
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

	ret = ml_parse_file(input, &phrase);

	if (ret < 0) {
		fprintf(stderr, "failed to initialize parser\n");
		ml_phrase_destroy(&phrase);
		fclose(input);
		return 2;
	}

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

	fclose(input);

	return ret ? 1 : 0;
}
