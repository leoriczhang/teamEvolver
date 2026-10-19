/** Read a File as base64 (strips the data URL prefix), mirroring console.html. */
export function fileToB64(file: File, onProgress?: (loaded: number, total: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = reject;
    if (onProgress) {
      r.onprogress = (event) => onProgress(event.loaded, event.lengthComputable ? event.total : file.size);
    }
    r.readAsDataURL(file);
  });
}
