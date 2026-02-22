# Partition table: SJ Research Nexus Disc Sharer

  * Disk location: 0x20000 bytes from start (256 512-byte sectors)
  * Length: 256 bytes
  * Identification: magic value `'Net1'` (hex `4E 65 74 31`) at offset zero

## "New format" partition table

The partition header is 16 bytes and starts at the beginning of the sector.

```
Ofs   Len   Content
0     4     Magic number. ASCII: "Net1"
4     1     Network number
5     1     ? (possibly disc sharer station number)
6     1     Delay Low
7     1     Delay High
8     8     0,0,0,0,0,0,0,0
```

Each partition entry occupies 16 bytes.

```
0     1     Flag (status)
1     1     Station
2     2     0,0
4     4     Address on disk (in sectors), ui32le, shifted left 1 bit
8     4     Size on disk (in sectors) and drive number, ui32le. Size is shifted left 1 bit. Drive number is stored in the MSbyte.
12    4     0,0,0,0
```

### Flags bitmap

  * 1: Writable (read-write by all users)
  * 2: Multiple (shared partition)
  * 4: Fixed (station number is nonzero)
  * 8: Printer (print spooler partition)
  * 16: Local (private partition, maximum of twelve)
  * 128: Last (last partition in table)

## "Old format" partition table

This is not documented as no Nexus disk images containing it are extant. Code to handle it is present in the SJ PartEdit source code, but
it immediately converts the table to the "new" format.

## References

  * SJ Research, !PartEdit source code, released on Stardot by ARG. `Cluster.Archie.PartEdit.Sources.c.Main`.

