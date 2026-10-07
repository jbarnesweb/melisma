#include <stdio.h>
#include <stdlib.h>

#include "melisma.h"

static int
test_destroy(void)
{
	struct ml_note_sequence notes = {
		.count = 2,
		.ticks_per_quarter = 480,
	};

	notes.notes = malloc(notes.count * sizeof(*notes.notes));
	if (!notes.notes) {
		perror("malloc");
		return 1;
	}

	ml_note_sequence_destroy(&notes);

	if (notes.notes != NULL ||
	    notes.count != 0 ||
	    notes.ticks_per_quarter != 0) {
		fprintf(stderr, "destroyed note sequence is not empty\n");
		return 1;
	}

	ml_note_sequence_destroy(&notes);

	if (notes.notes != NULL ||
	    notes.count != 0 ||
	    notes.ticks_per_quarter != 0) {
		fprintf(stderr, "re-destroyed note sequence is not empty\n");
		return 1;
	}

	return 0;
}

int
main(void)
{
	if (test_destroy())
		return 1;

	printf("note sequence tests passed\n");
	return 0;
}
