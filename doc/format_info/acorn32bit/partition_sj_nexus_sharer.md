# Partition table: SJ Research Nexus Disc Sharer

  * Disk location: 0x20000 bytes from start (256 512-byte sectors)
  * Length: 256 bytes
  * Identification: magic value `'Net1'` (hex `4E 65 74 31`) at offset zero

The region from byte 0x00 to 0x1FFFF (immediately before the partition table)
contains the disc sharer firmware/ROM image.

## "New format" partition table

The partition table begins with a 16-byte header, followed by up to 15
partition entries of 16 bytes each (240 bytes of entry space total).

```
Ofs   Len   Content
0     4     Magic number. ASCII: "Net1"
4     1     Network number
5     1     ? (possibly disc sharer station number; purpose unknown)
6     1     Delay Low
7     1     Delay High
8     8     0,0,0,0,0,0,0,0
```

Each partition entry occupies 16 bytes.

```
0     1     Flag (status)
1     1     Station
2     2     0,0
4     4     Address on disk, ui32le, in sectors.
8     4     Size on disk (in sectors) and drive number, ui32le. Size (in sectors)
            occupies bits 23–0 (i.e. word & 0x00FFFFFF). Drive number is
            in bits 31–24 (the MSbyte).
12    4     0,0,0,0
```

All sector addresses and sizes use 512-byte sectors.

ADFS-formatted partitions carry a Filecore boot block at
`partition_start + 0xC00`, from which the disc name can be retrieved.

### Flags bitmap

  * 1: Writable (read-write by all users)
  * 2: Multiple (shared partition)
  * 4: Fixed (station number field is nonzero)
  * 8: Printer (print spooler partition)
  * 16: Local (private partition, maximum of twelve)
  * 128: Last (this entry is the last valid partition; no further entries follow)

## "Old format" partition table

This is not documented as no Nexus disk images containing it are extant. Code to handle it is present in the SJ PartEdit source code, but
it immediately converts the table to the "new" format.

## References

  * SJ Research, !PartEdit source code, released on Stardot by ARG. `Cluster.Archie.PartEdit.Sources.c.Main`.

