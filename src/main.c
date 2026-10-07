#include <stdio.h>

#include "melisma_parser.tab.h"
#include "melisma_lexer.h"

int main(int argc, char **argv)
{
 yyscan_t scanner;
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

 if (yylex_init(&scanner) != 0) {
 fprintf(stderr, "failed to initialize scanner\n");
 fclose(input);
 return 2;
 }

 yyset_in(input, scanner);
 ret = yyparse(scanner);

 yylex_destroy(scanner);
 fclose(input);

 return ret ? 1 : 0;
}
