import SkillTransferModal from "./SkillTransferModal";

export default function ImportZipModal({ open, onClose, onImported }: {
  open: boolean; onClose: () => void; onImported: () => void;
}) {
  return <SkillTransferModal direction={open ? "import" : null} onClose={onClose} onImported={onImported} />;
}
