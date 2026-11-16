"""A window with no accessibility tree, for the live control tests.

Run as its own process, never imported: ``python probe_window.py <picture>
<clicks file> <title>``. It shows the picture at its own size and appends the
screen coordinates of every click to the file, which is what lets a test check
that clicking an icon by name lands inside that icon rather than merely
somewhere.

A separate process rather than a thread on purpose. Tk belongs to the thread
that created it, and an interpreter still alive at shutdown aborts the whole
process with "Tcl_AsyncDelete: async handler deleted by the wrong thread" --
which happens after the assertions have passed, and takes the report of them
with it.

Being a separate process is also how it can be left behind: a test that is
killed rather than finished never reaches the line that closes this one, and
the window then sits on the desktop until someone notices it. So it closes
itself after LIFETIME_MS whatever happens, and on Escape.
"""
import sys
import tkinter as tk

#: Longer than the test needs, short enough that a leftover is a nuisance for
#: a minute rather than until the next reboot.
LIFETIME_MS = 120_000


def main() -> None:
    picture, clicks, title = sys.argv[1], sys.argv[2], sys.argv[3]
    root = tk.Tk()
    root.title(title)
    root.geometry("+80+140")
    root.after(LIFETIME_MS, root.destroy)
    root.bind("<Escape>", lambda _event: root.destroy())
    image = tk.PhotoImage(file=picture)
    canvas = tk.Canvas(root, width=image.width(), height=image.height(),
                       highlightthickness=0)
    canvas.pack()
    canvas.create_image(0, 0, image=image, anchor="nw")

    def note(event: "tk.Event") -> None:
        with open(clicks, "a", encoding="utf-8") as handle:
            handle.write(f"{event.x_root} {event.y_root}\n")

    canvas.bind("<Button-1>", note)
    root.mainloop()


if __name__ == "__main__":
    main()
