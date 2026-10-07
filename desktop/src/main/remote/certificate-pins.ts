/** Explicit certificate exceptions approved by the user. Each pin requires both
 * the exact HTTPS origin and current SHA-256 fingerprint; none disables TLS
 * verification globally or modifies the operating system's trust store.
 */
export interface CertificatePin { origin: string; fingerprint: string }

export function parseCertificatePins(raw: string): CertificatePin[] {
  try {
    const values: unknown = JSON.parse(raw)
    if (!Array.isArray(values)) return []
    return values.flatMap((value) => {
      if (!value || typeof value.origin !== 'string' || typeof value.fingerprint !== 'string') return []
      const fingerprint = value.fingerprint.replace(/:/g, '').toLowerCase()
      if (!/^[a-f0-9]{64}$/.test(fingerprint)) return []
      try {
        const url = new URL(value.origin)
        if (url.protocol !== 'https:' || url.origin !== value.origin || url.username || url.password) return []
        return [{ origin: url.origin, fingerprint }]
      } catch { return [] }
    })
  } catch { return [] }
}

export function acceptsPinnedCertificate(pins: CertificatePin[], url: string, fingerprint: string): boolean {
  try {
    const target = new URL(url)
    return !target.username && !target.password && pins.some(pin => target.origin === pin.origin
      && fingerprint.replace(/:/g, '').toLowerCase() === pin.fingerprint)
  } catch { return false }
}
