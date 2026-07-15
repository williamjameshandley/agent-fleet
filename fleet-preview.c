#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vterm.h>

static VTerm *term;
static VTermScreen *screen;

static void utf8(uint32_t rune)
{
    if (rune < 0x80)
        putchar(rune);
    else if (rune < 0x800)
        printf("%c%c", 0xc0 | rune >> 6, 0x80 | (rune & 0x3f));
    else if (rune < 0x10000)
        printf("%c%c%c", 0xe0 | rune >> 12, 0x80 | (rune >> 6 & 0x3f),
               0x80 | (rune & 0x3f));
    else
        printf("%c%c%c%c", 0xf0 | rune >> 18, 0x80 | (rune >> 12 & 0x3f),
               0x80 | (rune >> 6 & 0x3f), 0x80 | (rune & 0x3f));
}

static int colour(const VTermColor *value, int foreground, int *sgr, int n)
{
    if ((foreground && VTERM_COLOR_IS_DEFAULT_FG(value)) ||
        (!foreground && VTERM_COLOR_IS_DEFAULT_BG(value))) {
        sgr[n++] = foreground ? 39 : 49;
    } else if (VTERM_COLOR_IS_INDEXED(value)) {
        int index = value->indexed.idx;
        if (index < 8)
            sgr[n++] = index + (foreground ? 30 : 40);
        else if (index < 16)
            sgr[n++] = index - 8 + (foreground ? 90 : 100);
        else {
            sgr[n++] = foreground ? 38 : 48;
            sgr[n++] = 5;
            sgr[n++] = index;
        }
    } else if (VTERM_COLOR_IS_RGB(value)) {
        sgr[n++] = foreground ? 38 : 48;
        sgr[n++] = 2;
        sgr[n++] = value->rgb.red;
        sgr[n++] = value->rgb.green;
        sgr[n++] = value->rgb.blue;
    }
    return n;
}

static void attributes(const VTermScreenCell *cell, const VTermScreenCell *old)
{
    int sgr[24], n = 0;
#define CHANGE(field, on, off) \
    do { if (!old->attrs.field && cell->attrs.field) sgr[n++] = on; \
         if (old->attrs.field && !cell->attrs.field) sgr[n++] = off; } while (0)
    CHANGE(bold, 1, 22);
    CHANGE(italic, 3, 23);
    CHANGE(blink, 5, 25);
    CHANGE(reverse, 7, 27);
    CHANGE(conceal, 8, 28);
    CHANGE(strike, 9, 29);
#undef CHANGE
    if (old->attrs.underline != cell->attrs.underline) {
        if (!cell->attrs.underline)
            sgr[n++] = 24;
        else
            sgr[n++] = cell->attrs.underline == VTERM_UNDERLINE_DOUBLE ? 21 : 4;
    }
    if (!vterm_color_is_equal(&old->fg, &cell->fg))
        n = colour(&cell->fg, 1, sgr, n);
    if (!vterm_color_is_equal(&old->bg, &cell->bg))
        n = colour(&cell->bg, 0, sgr, n);
    if (!n)
        return;
    printf("\033[%d", sgr[0]);
    for (int i = 1; i < n; i++)
        printf(";%d", sgr[i]);
    putchar('m');
}

static void draw(int top, int left, int rows, int cols, int cursor_y,
                 int cursor_x)
{
    VTermScreenCell old = {0};
    vterm_state_get_default_colors(vterm_obtain_state(term), &old.fg, &old.bg);
    for (int y = top; y < top + rows; y++) {
        for (int x = left; x < left + cols;) {
            VTermScreenCell cell;
            vterm_screen_get_cell(screen, (VTermPos){y, x}, &cell);
            if (y == cursor_y && x == cursor_x)
                cell.attrs.reverse = !cell.attrs.reverse;
            attributes(&cell, &old);
            if (cell.chars[0]) {
                for (int i = 0; i < VTERM_MAX_CHARS_PER_CELL && cell.chars[i]; i++)
                    utf8(cell.chars[i]);
            } else {
                for (int i = 0; i < cell.width; i++)
                    putchar(' ');
            }
            x += cell.width ? cell.width : 1;
            old = cell;
        }
        printf("\033[0m\n");
        memset(&old, 0, sizeof old);
        vterm_state_get_default_colors(vterm_obtain_state(term), &old.fg, &old.bg);
    }
}

static int origin(int cursor, int destination, int source)
{
    int start = cursor < destination / 3 ? 0 : cursor - destination / 3;
    if (start + destination > source)
        start = destination > source ? 0 : source - destination;
    return start;
}

int main(int argc, char **argv)
{
    if (argc != 7)
        return 2;
    int source_cols = atoi(argv[1]), source_rows = atoi(argv[2]);
    int cursor_x = atoi(argv[3]), cursor_y = atoi(argv[4]);
    int cols = atoi(argv[5]), rows = atoi(argv[6]);
    if (cols > source_cols) cols = source_cols;
    if (rows > source_rows) rows = source_rows;
    term = vterm_new(source_rows, source_cols);
    vterm_set_utf8(term, 1);
    screen = vterm_obtain_screen(term);
    vterm_screen_reset(screen, 1);

    char *line = NULL;
    size_t size = 0;
    int row = 1;
    while (getline(&line, &size, stdin) != -1 && row <= source_rows) {
        int length = strlen(line);
        if (length && line[length - 1] == '\n')
            line[--length] = '\0';
        char move[32];
        int move_length = snprintf(move, sizeof move, "\033[%d;1H", row++);
        vterm_input_write(term, move, move_length);
        vterm_input_write(term, line, length);
    }
    free(line);
    vterm_screen_flush_damage(screen);
    draw(origin(cursor_y, rows, source_rows),
         origin(cursor_x, cols, source_cols), rows, cols, cursor_y, cursor_x);
    vterm_free(term);
    return 0;
}
