#include <stdlib.h>

#include "melisma.h"

void
ml_note_sequence_destroy(struct ml_note_sequence *notes)
{
	free(notes->notes);

	notes->notes = NULL;
	notes->count = 0;
	notes->ticks_per_quarter = 0;
}
