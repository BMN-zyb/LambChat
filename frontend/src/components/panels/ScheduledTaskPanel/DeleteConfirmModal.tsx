import { useTranslation } from "react-i18next";
import { AlertCircle } from "lucide-react";

/** Delete confirmation modal */
export function DeleteConfirmModal({
  onConfirm,
  onCancel,
}: {
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();

  return (
    <>
      <div
        className="fixed inset-0 z-50 bg-black/50 transition-opacity"
        onClick={onCancel}
      />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div
          className="scheduled-task-modal max-w-md p-6"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-red-100 dark:bg-red-900/30">
            <AlertCircle className="text-red-600 dark:text-red-400" size={24} />
          </div>
          <h3 className="scheduled-task-modal__title">
            {t("scheduledTask.deleteConfirm")}
          </h3>
          <div className="mt-2">
            <p className="text-sm text-stone-500 dark:text-stone-400">
              {t("scheduledTask.deleteWarning")}
            </p>
          </div>
          <div className="mt-6 flex gap-3">
            <button
              onClick={onCancel}
              className="scheduled-task-button scheduled-task-button--secondary flex-1"
            >
              {t("scheduledTask.cancel")}
            </button>
            <button
              onClick={onConfirm}
              className="scheduled-task-button scheduled-task-button--danger flex-1"
            >
              {t("scheduledTask.delete")}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
