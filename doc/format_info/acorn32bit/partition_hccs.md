# Partition table: HCCS and Simtec IDEFS

  * Disk location: 0xC00 bytes from start (6 512-byte sectors)
    * Note: this is the Filecore boot block
  * Length: 512 bytes
  * Identification:
    * Simtec: magic value `'andy'` at offset 0x1B0
    * HCCS: magic value `'Andy'` at offset 0x1B0
  * Origin
    * Originally developed by Andy Armstrong at Wonderworks, for HCCS Associates.
    * A later version was provided to Simtec Electronics for their IDE interfaces, and added LBA addressing support.

## Partition table format

The partition table occupies part of the Hardware-dependent Information block in the [Filecore boot block](http://www.riscos.com/support/developers/prm/filecore.html#44373).
This means that partitions must follow each other, one after the other.
Non-Filecore partitions (e.g. FAT or RISC iX) are not supported.

Partition data begins at offset 0x1B0 into the Filecore boot sector.

```
Ofs   Len   Content
0     4     Magic number, "Andy" for HCCS, "andy" for Simtec.
4     8     Obfuscated password
12    2     Access Flags word, before password entered
14    2     Access Flags word, after password entered
```

### HCCS access flags words

The access flags words have the following format:

  * Bits 15..10: always 0
  * Bit 9: 1=Not mounted by default  (Simtec-only)
  * Bit 7: always 1
  * Bit 6: always 1
  * Bit 5: 1=Write access allowed
  * Bit 4: 1=Read access allowed
  * Bits 3..0: Always 0

These map to the [HCCS IDE formatter](https://chrisacorns.computinghistory.org.uk/docs/HCCS/HCCS_IDE_User_Guide.pdf#page=9) as the following values:

  * Allow read and write: `0xF0`
  * Allow read only: `0xD0`
  * Forbit access: `0xC0`

### Simtec access flag words

TODO.

### Partition sizing

For the earlier HCCS format, the partition size must be calculated from the [Filecore disc record](http://www.riscos.com/support/developers/prm/filecore.html#75310) stored between offsets `0x1C0` and `0x1FB` of the boot block.

For the later Simtec format, the partition size (in sectors) is optionally stored in the Disc Record:
(Credit: [Jon Abbott](https://forums.jaspp.org.uk/forum/viewtopic.php?p=4897#p4897))

```
Boot DiscRec:
Address :  0  1  2  3  4  5  6  7  8  9  A  B  C  D  E  F
&000000 : 09 FF FF 00 12 0D 00 02 01 C1 20 00 01 60 8F 01   .......... ..`..
&000010 : 00 00 70 72 00 00 52 69 73 63 50 43 0D 00 00 00   ..pr..RiscPC....
&000020 : CD 0F 00 00 07 00 00 00 01 01 03 00 01 00 00 00   ................
&000030 : 00 08 00 00 00 00 00 00 00 38 B9 03 42 FF FF 9B   .........8..B...
                                  ^^ ^^ ^^ ^^
```

If the middle two bytes of the last word (at 0x3C) are set to `FF FF`, and the last word isn't zero, then the partition size in bytes `0x38..0x3B` is considered valid.


### Password obfuscation

HCCS passwords are obfuscated by XORing each byte with a fixed key:

```
void hccs_pwendec(char *pwd)
{
    uint8_t XORTAB[8] = { 0x06, 0x14, 0x1F, 0x07, 0x02, 0x1D, 0x17, 0x17 };
    for (size_t i=0; i<8; i++) {
        pwd[i] ^= XORTAB[i];
    }
}
```

The algorithm for the Simtec version is unknown.

