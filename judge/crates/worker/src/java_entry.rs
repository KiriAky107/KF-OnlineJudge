//! Bounded metadata inspection of sealed javac output, never class loading.
//! Format: https://docs.oracle.com/javase/specs/jvms/se21/html/jvms-4.html
//! Bytecode verification and execution remain inside the isolated JVM.
use std::collections::{BTreeMap, HashSet};
struct Reader<'a>(&'a [u8]);
impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], &'static str> {
        let value = self.0.get(..n).ok_or("truncated class metadata")?;
        self.0 = &self.0[n..];
        Ok(value)
    }
    fn u8(&mut self) -> Result<u8, &'static str> {
        Ok(self.take(1)?[0])
    }
    fn u16(&mut self) -> Result<u16, &'static str> {
        Ok(u16::from_be_bytes(
            self.take(2)?.try_into().expect("two bytes"),
        ))
    }
    fn u32(&mut self) -> Result<u32, &'static str> {
        Ok(u32::from_be_bytes(
            self.take(4)?.try_into().expect("four bytes"),
        ))
    }
    fn attributes(&mut self, pool: &[Constant<'a>]) -> Result<(), &'static str> {
        for _ in 0..self.u16()? {
            utf8(pool, self.u16()?)?;
            let length = self.u32()? as usize;
            self.take(length)?;
        }
        Ok(())
    }
}
enum Constant<'a> {
    Other,
    Utf8(&'a [u8]),
    Class(u16),
}
fn utf8<'a>(pool: &[Constant<'a>], index: u16) -> Result<&'a [u8], &'static str> {
    match pool.get(usize::from(index)) {
        Some(Constant::Utf8(bytes)) if index != 0 => Ok(bytes),
        _ => Err("invalid class UTF8 reference"),
    }
}
fn class<'a>(pool: &[Constant<'a>], index: u16) -> Result<&'a [u8], &'static str> {
    match pool.get(usize::from(index)) {
        Some(Constant::Class(name)) if index != 0 => utf8(pool, *name),
        _ => Err("invalid class name reference"),
    }
}
struct Info<'a> {
    parent: &'a [u8],
    main: bool,
}
fn parse<'a>(path: &str, data: &'a [u8]) -> Result<Info<'a>, &'static str> {
    if data.len() as u64 > kf_contracts::judging::MAX_PACKAGE_BYTES {
        return Err("class metadata byte budget");
    }
    let mut r = Reader(data);
    if r.u32()? != 0xcafebabe || r.u16()? != 0 || r.u16()? != 65 {
        return Err("class differs from fixed javac release");
    }
    let count = usize::from(r.u16()?);
    if count < 2 {
        return Err("empty class constant pool");
    }
    let mut pool = vec![Constant::Other];
    while pool.len() < count {
        let c = match r.u8()? {
            1 => {
                let n = usize::from(r.u16()?);
                Constant::Utf8(r.take(n)?)
            }
            7 => Constant::Class(r.u16()?),
            3 | 4 | 9 | 10 | 11 | 12 | 17 | 18 => {
                r.take(4)?;
                Constant::Other
            }
            8 | 16 | 19 | 20 => {
                r.take(2)?;
                Constant::Other
            }
            15 => {
                r.take(3)?;
                Constant::Other
            }
            5 | 6 => {
                r.take(8)?;
                if pool.len() + 1 >= count {
                    return Err("wide constant slot budget");
                }
                pool.push(Constant::Other);
                Constant::Other
            }
            _ => return Err("unknown class constant"),
        };
        pool.push(c);
    }
    r.u16()?;
    if class(&pool, r.u16()?)?
        != path
            .strip_suffix(".class")
            .ok_or("class file path")?
            .as_bytes()
    {
        return Err("class identity differs from sealed file");
    }
    let parent = class(&pool, r.u16()?)?;
    for _ in 0..r.u16()? {
        class(&pool, r.u16()?)?;
    }
    for _ in 0..r.u16()? {
        r.u16()?;
        utf8(&pool, r.u16()?)?;
        utf8(&pool, r.u16()?)?;
        r.attributes(&pool)?;
    }
    let mut main = false;
    for _ in 0..r.u16()? {
        let flags = r.u16()?;
        let name = utf8(&pool, r.u16()?)?;
        let descriptor = utf8(&pool, r.u16()?)?;
        main |=
            flags & 0x0009 == 0x0009 && name == b"main" && descriptor == b"([Ljava/lang/String;)V";
        r.attributes(&pool)?;
    }
    r.attributes(&pool)?;
    if !r.0.is_empty() {
        return Err("trailing class metadata");
    }
    Ok(Info { parent, main })
}
pub(crate) fn has_main(files: &[(String, Vec<u8>)]) -> Result<bool, &'static str> {
    if files.is_empty()
        || files.len() > kf_contracts::judging_v2::MAX_CLASS_FILES
        || files.iter().map(|(_, d)| d.len() as u64).sum::<u64>()
            > kf_contracts::judging::MAX_PACKAGE_BYTES
    {
        return Err("class collection metadata budget");
    }
    let mut classes = BTreeMap::new();
    for (path, bytes) in files {
        if !kf_contracts::judging_v2::class_name(path) {
            return Err("unsupported class path");
        }
        if classes
            .insert(
                path.strip_suffix(".class").expect("class path").as_bytes(),
                parse(path, bytes)?,
            )
            .is_some()
        {
            return Err("duplicate class identity");
        }
    }
    let mut name = &b"Main"[..];
    let mut seen = HashSet::new();
    while let Some(info) = classes.get(name) {
        if !seen.insert(name) {
            return Err("class superclass cycle");
        }
        if info.main {
            return Ok(true);
        }
        name = info.parent;
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn minimal(flags: u16, descriptor: &[u8]) -> Vec<u8> {
        let mut b = vec![0xca, 0xfe, 0xba, 0xbe, 0, 0, 0, 65, 0, 7];
        for name in [
            &b"Main"[..],
            &b"java/lang/Object"[..],
            &b"main"[..],
            descriptor,
        ] {
            b.push(1);
            b.extend_from_slice(&(name.len() as u16).to_be_bytes());
            b.extend_from_slice(name);
        }
        b.extend_from_slice(&[7, 0, 1, 7, 0, 2, 0, 0x21, 0, 5, 0, 6, 0, 0, 0, 0, 0, 1]);
        b.extend_from_slice(&flags.to_be_bytes());
        b.extend_from_slice(&[0, 3, 0, 4, 0, 0, 0, 0]);
        b
    }
    #[test]
    fn entry_signature_and_truncation_are_checked_without_loading_classes() {
        let yes = minimal(9, b"([Ljava/lang/String;)V");
        assert!(has_main(&[("Main.class".into(), yes.clone())]).unwrap());
        for flags in [0, 1, 8] {
            assert!(
                !has_main(&[(
                    "Main.class".into(),
                    minimal(flags, b"([Ljava/lang/String;)V")
                )])
                .unwrap()
            );
        }
        assert!(!has_main(&[("Main.class".into(), minimal(9, b"()V"))]).unwrap());
        for n in 0..yes.len() {
            assert!(parse("Main.class", &yes[..n]).is_err());
        }
        assert!(parse("Other.class", &yes).is_err());
        let mut extra = yes;
        extra.push(0);
        assert!(parse("Main.class", &extra).is_err());
    }
}
